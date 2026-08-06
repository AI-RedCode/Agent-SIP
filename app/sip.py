from __future__ import annotations

import asyncio
import audioop
import contextlib
import hashlib
import logging
import random
import re
import secrets
import socket
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

from .calls import SIPTransportNotReadyError
from .realtime import pcm24_to_telephony, telephony_to_pcm24

log = logging.getLogger(__name__)

SIP_ALLOW_METHODS = "INVITE, ACK, BYE, CANCEL, OPTIONS, REGISTER"
INBOUND_REALTIME_TIMEOUT = 5


def pcmu_encode(pcm: bytes) -> bytes: return audioop.lin2ulaw(pcm, 2)
def pcmu_decode(data: bytes) -> bytes: return audioop.ulaw2lin(data, 2)
def pcma_encode(pcm: bytes) -> bytes: return audioop.lin2alaw(pcm, 2)
def pcma_decode(data: bytes) -> bytes: return audioop.alaw2lin(data, 2)


def parse_message(data: bytes) -> tuple[str, dict[str, str], str]:
    text = data.decode(errors="replace")
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower().strip()] = value.strip()
    return lines[0], headers, body


def digest_authorization(challenge: str, username: str, password: str, method: str, uri: str) -> str:
    values = dict(re.findall(r'(\w+)="?([^",]+)', challenge))
    realm, nonce = values.get("realm", ""), values.get("nonce", "")
    md5 = lambda s: hashlib.md5(s.encode()).hexdigest()
    response = md5(f"{md5(f'{username}:{realm}:{password}')}:{nonce}:{md5(f'{method}:{uri}')}" )
    return f'Digest username="{username}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{response}", algorithm=MD5'


@dataclass
class Dialog:
    addr: tuple[str, int]
    call_id: str
    from_header: str
    to_header: str
    cseq: int
    remote_rtp: tuple[str, int] | None = None
    invite_branch: str = ""


class RTPProtocol(asyncio.DatagramProtocol):
    PACKET_SIZE = 160
    PACKET_INTERVAL = 0.020
    MAX_QUEUED_PACKETS = 1000
    QUEUE_WARNING_INTERVAL = 5.0

    def __init__(self, ua):
        self.ua, self.transport, self.seq, self.timestamp, self.ssrc = ua, None, random.randrange(65536), random.randrange(2**32), random.randrange(2**32)
        self.send_queue = asyncio.Queue(maxsize=self.MAX_QUEUED_PACKETS)
        self.sender_task = None
        self._send_resample_state = None
        self._receive_resample_state = None
        self._send_remainder = b""
        self._last_queue_warning = float("-inf")
        self._ambient_pcm = self._load_ambient()
        self._ambient_position = 0
    def _load_ambient(self):
        config = self.ua.ambient
        if not config.enabled:
            return b""
        path = Path(__file__).parent.parent / "assets" / "ambient" / config.file
        try:
            with wave.open(str(path), "rb") as source:
                channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
                pcm = source.readframes(source.getnframes())
            if channels == 2:
                pcm = audioop.tomono(pcm, width, 0.5, 0.5)
            elif channels != 1:
                raise ValueError("only mono or stereo WAV files are supported")
            if width != 2:
                pcm = audioop.lin2lin(pcm, width, 2)
            if rate != 8000:
                pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 8000, None)
            return pcm
        except (OSError, EOFError, wave.Error, ValueError) as exc:
            log.warning("Could not load ambient sound %s: %s", path, exc)
            return b""
    @property
    def ambient_active(self):
        return bool(self._ambient_pcm and self.ua.ambient.enabled and self.ua.ambient.volume > 0)
    def _ambient_frame(self, sample_count):
        byte_count = sample_count * 2
        parts = []
        while byte_count:
            take = min(byte_count, len(self._ambient_pcm) - self._ambient_position)
            parts.append(self._ambient_pcm[self._ambient_position:self._ambient_position + take])
            self._ambient_position = (self._ambient_position + take) % len(self._ambient_pcm)
            byte_count -= take
        return b"".join(parts)
    def mix_pcm16(self, voice: bytes) -> bytes:
        if not self.ambient_active:
            return voice
        if not voice:
            voice = b"\x00" * (self.PACKET_SIZE * 2)
        ambient = audioop.mul(self._ambient_frame(len(voice) // 2), 2, self.ua.ambient.volume)
        return audioop.add(voice, ambient, 2)
    def _mix_payload(self, payload: bytes) -> bytes:
        decode = pcmu_decode if self.ua.config.codec == "pcmu" else pcma_decode
        encode = pcmu_encode if self.ua.config.codec == "pcmu" else pcma_encode
        return encode(self.mix_pcm16(decode(payload)))
    def start_sender(self):
        if self.ambient_active and (not self.sender_task or self.sender_task.done()):
            self.sender_task = asyncio.create_task(self._send_packets())
    def connection_made(self, transport): self.transport = transport
    def datagram_received(self, data, addr):
        if len(data) >= 12 and self.ua.manager.active and self.ua.manager.active.realtime:
            pcm, self._receive_resample_state = telephony_to_pcm24(
                data[12:], self.ua.config.codec, self._receive_resample_state, return_state=True
            )
            asyncio.create_task(self.ua.manager.active.realtime.send_audio(pcm))
    async def send_pcm24(self, pcm: bytes):
        if not pcm and not self.ambient_active:
            return
        if not self.transport or not self.ua.dialog or not self.ua.dialog.remote_rtp: return
        if pcm:
            payload, self._send_resample_state = pcm24_to_telephony(
                pcm, self.ua.config.codec, self._send_resample_state, return_state=True
            )
        else:
            payload = b"\xff" * self.PACKET_SIZE if self.ua.config.codec == "pcmu" else b"\xd5" * self.PACKET_SIZE
        payload = self._send_remainder + payload
        packet_end = len(payload) - (len(payload) % self.PACKET_SIZE)
        self._send_remainder = payload[packet_end:]
        for pos in range(0, packet_end, self.PACKET_SIZE):
            if self.send_queue.full():
                self.send_queue.get_nowait()
                self.send_queue.task_done()
                now = asyncio.get_running_loop().time()
                if now - self._last_queue_warning >= self.QUEUE_WARNING_INTERVAL:
                    log.warning("RTP send queue full; dropping oldest audio packet")
                    self._last_queue_warning = now
            self.send_queue.put_nowait(payload[pos:pos + self.PACKET_SIZE])
        if not self.sender_task or self.sender_task.done():
            self.sender_task = asyncio.create_task(self._send_packets())

    async def _send_packets(self):
        loop = asyncio.get_running_loop()
        next_deadline = loop.time() + self.PACKET_INTERVAL
        try:
            while True:
                if self.ambient_active and not (
                    self.transport and self.ua.dialog and self.ua.dialog.remote_rtp
                ):
                    return
                queued = True
                if self.ambient_active:
                    try:
                        chunk = await asyncio.wait_for(self.send_queue.get(), self.PACKET_INTERVAL)
                    except TimeoutError:
                        queued = False
                        silence = b"\xff" * self.PACKET_SIZE if self.ua.config.codec == "pcmu" else b"\xd5" * self.PACKET_SIZE
                        chunk = silence
                    chunk = self._mix_payload(chunk)
                else:
                    chunk = await self.send_queue.get()
                dialog = self.ua.dialog
                if self.transport and dialog and dialog.remote_rtp:
                    header = struct.pack("!BBHII", 0x80, 0 if self.ua.config.codec == "pcmu" else 8, self.seq, self.timestamp, self.ssrc)
                    self.transport.sendto(header + chunk, dialog.remote_rtp)
                    self.seq, self.timestamp = (self.seq + 1) & 0xffff, (self.timestamp + len(chunk)) & 0xffffffff
                if queued:
                    self.send_queue.task_done()
                delay = next_deadline - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    # Do not catch up at burst speed after the queue was idle or
                    # the event loop was delayed for more than one packet period.
                    next_deadline = loop.time()
                next_deadline += self.PACKET_INTERVAL
        except asyncio.CancelledError:
            raise

    async def drain(self, timeout: float = 5.0):
        """Wait for queued RTP audio to be sent, without delaying forever."""
        try:
            await asyncio.wait_for(self.send_queue.join(), timeout)
        except TimeoutError:
            log.warning("Timed out waiting for RTP send queue to drain")

    async def stop_sender(self):
        if self.sender_task:
            self.sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.sender_task
            self.sender_task = None
        while not self.send_queue.empty():
            self.send_queue.get_nowait()
            self.send_queue.task_done()
        self._send_remainder = b""
        self._send_resample_state = None
        self._receive_resample_state = None


class SIPUserAgent(asyncio.DatagramProtocol):
    def __init__(self, config, manager, ambient=None):
        self.config, self.manager = config, manager
        if ambient is None:
            from .config import AmbientConfig
            ambient = AmbientConfig()
        self.ambient = ambient
        try:
            address = socket.getaddrinfo(
                config.server_host,
                config.server_port,
                family=socket.AF_INET,
                type=socket.SOCK_DGRAM,
            )[0][4]
            self.server = (address[0], address[1])
        except (OSError, IndexError) as exc:
            log.warning(
                "Could not resolve SIP server %s:%s; using hostname: %s",
                config.server_host,
                config.server_port,
                exc,
            )
            self.server = (config.server_host, config.server_port)
        self.transport = None
        self.registered = False
        self.cseq = 1
        self.dialog: Dialog | None = None
        self.rtp: RTPProtocol | None = None
        self.rtp_transport = None
        self.refresh_task = None
        self.register_timeout_task = None
        self.register_attempt = 0
        # A REGISTER authentication retry is part of the same registrar
        # transaction/dialog.  Keep these values stable for the lifetime of
        # the UA instead of inventing a new identity after every challenge.
        self.register_call_id = secrets.token_hex(12)
        self.register_tag = self._tag()
        self.register_challenge: str | None = None
        self._message_tasks: set[asyncio.Task] = set()

    def connection_made(self, transport): self.transport = transport

    def _branch(self): return "z9hG4bK" + secrets.token_hex(8)
    def _tag(self): return secrets.token_hex(6)
    def _contact(self): return f"<sip:{self.config.username}@{self.config.advertise_host}:{self.config.local_port}>"
    def _request(self, method, uri, headers, body=""):
        base = [f"{method} {uri} SIP/2.0", *[f"{k}: {v}" for k, v in headers.items()], f"Content-Length: {len(body.encode())}", "", body]
        return "\r\n".join(base).encode()

    def build_register(self, authorization: str | None = None, expires: int = 300) -> bytes:
        uri = f"sip:{self.config.server_host}"
        headers = {"Via": f"SIP/2.0/UDP {self.config.advertise_host}:{self.config.local_port};branch={self._branch()};rport", "From": f"<sip:{self.config.username}@{self.config.server_host}>;tag={self.register_tag}", "To": f"<sip:{self.config.username}@{self.config.server_host}>", "Call-ID": self.register_call_id, "CSeq": f"{self.cseq} REGISTER", "Contact": self._contact(), "Expires": str(expires), "Max-Forwards": "70"}
        if authorization: headers["Authorization"] = authorization
        self.cseq += 1
        return self._request("REGISTER", uri, headers)

    async def start(self):
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, local_addr=(self.config.local_host, self.config.local_port))
        for port in range(self.config.rtp_port_start, self.config.rtp_port_end + 1, 2):
            try:
                self.rtp_transport, self.rtp = await loop.create_datagram_endpoint(lambda: RTPProtocol(self), local_addr=(self.config.local_host, port))
                self.rtp_port = port; break
            except OSError: continue
        if not self.rtp: raise RuntimeError("no RTP port available")
        self._send_register(self.build_register())
        self.refresh_task = asyncio.create_task(self._refresh())

    async def _refresh(self):
        while True:
            await asyncio.sleep(240)
            if self.transport:
                self.registered = False
                self._send_register(self._authenticated_register())

    def _send_register(self, request: bytes, expires: int = 300):
        self.transport.sendto(request, self.server)
        log.info("SIP REGISTER sent to %s (expires=%s)", self.config.server_host, expires)
        self.register_attempt += 1
        if self.register_timeout_task:
            self.register_timeout_task.cancel()
        self.register_timeout_task = asyncio.create_task(self._register_timeout(self.register_attempt))

    async def _register_timeout(self, attempt: int):
        try:
            await asyncio.sleep(5)
            if attempt == self.register_attempt and not self.registered:
                log.warning("SIP REGISTER timed out (no response from %s)", self.config.server_host)
        except asyncio.CancelledError:
            pass

    def _cancel_register_timeout(self):
        if self.register_timeout_task:
            self.register_timeout_task.cancel()
            self.register_timeout_task = None

    def _authenticated_register(self, expires: int = 300) -> bytes:
        auth = None
        if self.register_challenge:
            uri = f"sip:{self.config.server_host}"
            auth = digest_authorization(self.register_challenge, self.config.auth_username or self.config.username, self.config.password, "REGISTER", uri)
        return self.build_register(auth, expires)

    async def stop(self):
        if self.refresh_task: self.refresh_task.cancel()
        self._cancel_register_timeout()
        if self.transport:
            request = self._authenticated_register(expires=0)
            self.transport.sendto(request, self.server)
            log.info("SIP REGISTER sent to %s (expires=0)", self.config.server_host)
            self.transport.close()
        if self.rtp: await self.rtp.stop_sender()
        if self.rtp_transport: self.rtp_transport.close()
        self.registered = False

    def datagram_received(self, data, addr):
        task = asyncio.create_task(self._handle_message_safely(data, addr))
        self._message_tasks.add(task)
        task.add_done_callback(self._message_tasks.discard)

    async def _handle_message_safely(self, data: bytes, addr):
        try:
            await self.handle_message(data, addr)
        except asyncio.CancelledError:
            raise
        except Exception:
            # DatagramProtocol callbacks must never leak application failures:
            # one failed Realtime session must not poison later SIP traffic.
            log.exception("Failed to handle SIP datagram from %s:%s", *addr)

    def error_received(self, exc):
        log.warning("SIP UDP transport error: %s", exc)

    def connection_lost(self, exc):
        self.transport = None
        self.registered = False
        if exc:
            log.error("SIP UDP transport lost: %s", exc)

    async def handle_message(self, data: bytes, addr):
        start, h, body = parse_message(data)
        if start.startswith("SIP/2.0 401") and h.get("cseq", "").endswith("REGISTER") and "www-authenticate" in h:
            uri = f"sip:{self.config.server_host}"
            self.register_challenge = h["www-authenticate"]
            auth_username = self.config.auth_username or self.config.username
            log.info("SIP REGISTER challenged, authenticating as %s", auth_username)
            auth = digest_authorization(self.register_challenge, auth_username, self.config.password, "REGISTER", uri)
            self._send_register(self.build_register(auth))
        elif start.startswith("SIP/2.0 200") and h.get("cseq", "").endswith("REGISTER"):
            self.registered = True
            self._cancel_register_timeout()
            log.info("SIP REGISTER OK — registered as %s", self.config.username)
        elif start.startswith(("SIP/2.0 403", "SIP/2.0 404")) and h.get("cseq", "").endswith("REGISTER"):
            self.registered = False
            self._cancel_register_timeout()
            status = start.split()[1]
            log.error("SIP REGISTER rejected (%s) — check username/password", status)
        elif start.startswith("INVITE "):
            await self._inbound(h, body, addr)
        elif start.startswith("SIP/2.0 200") and h.get("cseq", "").endswith("INVITE"):
            await self._outbound_answer(h, body, addr)
        elif start.startswith(("SIP/2.0 401", "SIP/2.0 407")) and h.get("cseq", "").endswith("INVITE"):
            await self._outbound_authenticate(h, addr, start.startswith("SIP/2.0 407"))
        elif start.startswith("SIP/2.0 180") and h.get("cseq", "").endswith("INVITE"):
            await self._outbound_ringing()
        elif start.startswith("BYE "):
            self._respond(200, "OK", h, addr); await self.manager.hangup(remote=True); self.dialog = None
        elif start.startswith("OPTIONS "):
            self._respond(200, "OK", h, addr, extra={
                "Contact": self._contact(),
                "Allow": SIP_ALLOW_METHODS,
            })
        elif start.startswith("ACK "):
            # ACK is never answered: it completes an INVITE transaction.
            return
        elif not start.startswith("SIP/2.0 "):
            self._respond(501, "Not Implemented", h, addr)

    def _respond(self, code, reason, h, addr, body="", extra=None):
        headers = {"Via": h.get("via", ""), "From": h.get("from", ""), "To": h.get("to", "") + (";tag=" + self._tag() if "tag=" not in h.get("to", "") else ""), "Call-ID": h.get("call-id", ""), "CSeq": h.get("cseq", "")}
        headers.update(extra or {})
        wire = "\r\n".join([f"SIP/2.0 {code} {reason}", *[f"{k}: {v}" for k,v in headers.items()], f"Content-Length: {len(body)}", "", body]).encode()
        self.transport.sendto(wire, addr)

    def _sdp(self):
        pt, name = (0, "PCMU") if self.config.codec == "pcmu" else (8, "PCMA")
        return f"v=0\r\no=- 0 0 IN IP4 {self.config.advertise_host}\r\ns=Agent-SIP\r\nc=IN IP4 {self.config.advertise_host}\r\nt=0 0\r\nm=audio {self.rtp_port} RTP/AVP {pt}\r\na=rtpmap:{pt} {name}/8000\r\na=sendrecv\r\n"

    def _remote_rtp(self, body):
        host = re.search(r"c=IN IP4 ([^\s]+)", body); port = re.search(r"m=audio (\d+)", body)
        return (host.group(1), int(port.group(1))) if host and port else None

    async def _inbound(self, h, body, addr):
        if self.manager.active:
            if (
                self.manager.active.direction == "inbound"
                and self.manager.active.state == "ringing"
                and self.manager.active.call_id == h.get("call-id")
            ):
                self._respond(180, "Ringing", h, addr)
                return
            self._respond(486, "Busy Here", h, addr)
            return
        number = re.search(r"sip:([^@>;]+)", h.get("from", ""))
        caller = number.group(1) if number else "unknown"
        log.info("INBOUND call from %s", caller)
        session = await self.manager.start(h.get("call-id", secrets.token_hex(8)), "inbound", caller)
        self.dialog = Dialog(addr, session.call_id, h.get("from", ""), h.get("to", ""), int(h.get("cseq", "1 ").split()[0]), self._remote_rtp(body))
        if self.rtp:
            self.rtp.start_sender()
        self._respond(180, "Ringing", h, addr)
        try:
            await asyncio.wait_for(
                self.manager.prepare_realtime(session),
                timeout=INBOUND_REALTIME_TIMEOUT,
            )
        except TimeoutError:
            log.warning(
                "Realtime session was not ready within %ss; answering inbound call anyway",
                INBOUND_REALTIME_TIMEOUT,
            )
        except Exception as exc:
            log.warning("Realtime session failed before inbound answer; answering anyway: %s", exc)

        if session.realtime:
            await session.realtime.greet()
        sdp = self._sdp()
        self._respond(200, "OK", h, addr, sdp, {"Contact": self._contact(), "Content-Type": "application/sdp"})
        await self.manager.answered(session)

    async def invite(self, number):
        log.info("OUTBOUND call to %s", number)
        session = await self.manager.start(secrets.token_hex(12), "outbound", number)
        if self.transport is None:
            await self.manager.hangup(remote=True)
            raise SIPTransportNotReadyError("SIP transport not ready")
        uri = f"sip:{number}@{self.config.server_host}"
        tag, branch = self._tag(), self._branch(); headers = {"Via": f"SIP/2.0/UDP {self.config.advertise_host}:{self.config.local_port};branch={branch};rport", "From": f'"{self.manager.agent_name}" <sip:{self.manager.caller_id}@{self.config.server_host}>;tag={tag}', "To": f"<sip:{number}@{self.config.server_host}>", "Call-ID": session.call_id, "CSeq": f"{self.cseq} INVITE", "Contact": self._contact(), "Max-Forwards": "70", "Content-Type": "application/sdp"}
        self.dialog = Dialog(self.server, session.call_id, headers["From"], headers["To"], self.cseq, invite_branch=branch); self.cseq += 1
        self.transport.sendto(self._request("INVITE", uri, headers, self._sdp()), self.server)
        session.ring_timeout_task = asyncio.create_task(self._ring_timeout(session))
        return session

    async def _ring_timeout(self, session):
        try:
            await asyncio.sleep(self.manager.max_ring_seconds)
            await self._end_not_answered(session)
        except asyncio.CancelledError:
            pass

    async def _end_not_answered(self, session):
        if session.ending or self.manager.active is not session or session.state != "ringing":
            return
        session.ending = True
        log.warning("OUTBOUND call to %s not answered after %s rings", session.number, session.rings)
        await self.cancel(session)
        await self.manager.hangup(remote=True, outcome="not_answered")

    async def _outbound_answer(self, h, body, addr):
        if not self.manager.active or self.manager.active.state != "ringing" or not self.dialog: return
        log.info("OUTBOUND call to %s answered", self.manager.active.number)
        self.dialog.addr, self.dialog.remote_rtp, self.dialog.to_header = addr, self._remote_rtp(body), h.get("to", self.dialog.to_header)
        if self.rtp:
            self.rtp.start_sender()
        uri = re.search(r"<([^>]+)>", h.get("contact", "")); uri = uri.group(1) if uri else f"sip:{self.manager.active.number}@{self.config.server_host}"
        headers = {"Via": f"SIP/2.0/UDP {self.config.advertise_host}:{self.config.local_port};branch={self._branch()}", "From": self.dialog.from_header, "To": self.dialog.to_header, "Call-ID": self.dialog.call_id, "CSeq": f"{self.dialog.cseq} ACK", "Max-Forwards": "70"}
        self.transport.sendto(self._request("ACK", uri, headers), addr); await self.manager.answered(self.manager.active)

    async def _outbound_authenticate(self, h, addr, proxy=False):
        session = self.manager.active
        challenge_header = "proxy-authenticate" if proxy else "www-authenticate"
        if not session or session.direction != "outbound" or not self.dialog or challenge_header not in h:
            return
        uri = f"sip:{session.number}@{self.config.server_host}"
        # A final non-2xx INVITE response must be ACKed before starting the
        # authenticated INVITE transaction.
        ack_headers = {
            "Via": f"SIP/2.0/UDP {self.config.advertise_host}:{self.config.local_port};branch={self.dialog.invite_branch};rport",
            "From": self.dialog.from_header,
            "To": h.get("to", self.dialog.to_header),
            "Call-ID": self.dialog.call_id,
            "CSeq": f"{self.dialog.cseq} ACK",
            "Max-Forwards": "70",
        }
        self.transport.sendto(self._request("ACK", uri, ack_headers), addr)
        self.dialog.cseq += 1
        self.dialog.invite_branch = self._branch()
        auth = digest_authorization(
            h[challenge_header],
            self.config.auth_username or self.config.username,
            self.config.password,
            "INVITE",
            uri,
        )
        headers = {
            "Via": f"SIP/2.0/UDP {self.config.advertise_host}:{self.config.local_port};branch={self.dialog.invite_branch};rport",
            "From": self.dialog.from_header,
            "To": self.dialog.to_header,
            "Call-ID": self.dialog.call_id,
            "CSeq": f"{self.dialog.cseq} INVITE",
            "Contact": self._contact(),
            "Max-Forwards": "70",
            "Content-Type": "application/sdp",
            "Proxy-Authorization" if proxy else "Authorization": auth,
        }
        self.transport.sendto(self._request("INVITE", uri, headers, self._sdp()), addr)

    async def _outbound_ringing(self):
        session = self.manager.active
        if not session or session.direction != "outbound" or session.state != "ringing":
            return
        session.rings += 1
        log.info("OUTBOUND call to %s ringing", session.number)
        if session.rings >= self.manager.max_rings:
            await self._end_not_answered(session)

    async def cancel(self, session):
        if not self.dialog or not self.transport:
            return
        uri = f"sip:{session.number}@{self.config.server_host}"
        headers = {"Via": f"SIP/2.0/UDP {self.config.advertise_host}:{self.config.local_port};branch={self.dialog.invite_branch};rport", "From": self.dialog.from_header, "To": self.dialog.to_header, "Call-ID": self.dialog.call_id, "CSeq": f"{self.dialog.cseq} CANCEL", "Max-Forwards": "70"}
        self.transport.sendto(self._request("CANCEL", uri, headers), self.dialog.addr)
        self.dialog = None

    async def bye(self, session):
        if not self.dialog or not self.transport: return
        uri = f"sip:{session.number}@{self.config.server_host}"
        headers = {"Via": f"SIP/2.0/UDP {self.config.advertise_host}:{self.config.local_port};branch={self._branch()}", "From": self.dialog.from_header, "To": self.dialog.to_header, "Call-ID": self.dialog.call_id, "CSeq": f"{self.dialog.cseq + 1} BYE", "Max-Forwards": "70"}
        self.transport.sendto(self._request("BYE", uri, headers), self.dialog.addr); self.dialog = None
