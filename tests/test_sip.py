import asyncio
import contextlib
import ipaddress
import struct

import pytest

from app.calls import CallManager
from app.config import AmbientConfig, SIPConfig
from app.sip import Dialog, RTPProtocol, SIPUserAgent, digest_authorization


class Transport:
    def __init__(self): self.sent=[]; self.closed=False
    def sendto(self, data, addr): self.sent.append((data,addr))
    def close(self): self.closed=True


INVITE=("INVITE sip:200@pbx SIP/2.0\r\nVia: SIP/2.0/UDP 10.0.0.2:5060;branch=z\r\nFrom: <sip:201@pbx>;tag=a\r\nTo: <sip:200@pbx>\r\nCall-ID: call1\r\nCSeq: 1 INVITE\r\nContent-Type: application/sdp\r\nContent-Length: 73\r\n\r\nv=0\r\nc=IN IP4 10.0.0.2\r\nm=audio 4000 RTP/AVP 0\r\n").encode()


def ua():
    calls=CallManager(); calls.agent_name="Agent"; calls.caller_id="200"
    u=SIPUserAgent(SIPConfig(server_host="pbx", advertise_host="10.0.0.3"),calls); u.transport=Transport(); u.rtp_port=40000; calls.sip=u
    return u,calls


@pytest.mark.asyncio
async def test_rtp_audio_is_queued_and_sent_in_order(monkeypatch):
    u, _ = ua()
    u.dialog = Dialog(("10.0.0.2", 5060), "call", "from", "to", 1, ("10.0.0.2", 4000))
    rtp = RTPProtocol(u); rtp.transport = Transport()
    start_seq, start_timestamp = rtp.seq, rtp.timestamp
    sleeps = []

    async def paced_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("app.sip.asyncio.sleep", paced_sleep)
    # 960 PCM16 samples at 24 kHz becomes two 160-byte PCMU packets.
    await rtp.send_pcm24(b"\x00\x00" * 960)
    assert rtp.transport.sent == []
    with contextlib.suppress(asyncio.CancelledError):
        await rtp.sender_task

    assert len(sleeps) == 2
    assert all(delay > 0 for delay in sleeps)
    assert len(rtp.transport.sent) == 2
    headers = [struct.unpack("!BBHII", packet[:12]) for packet, _ in rtp.transport.sent]
    assert [header[2] for header in headers] == [start_seq, (start_seq + 1) & 0xffff]
    assert [header[3] for header in headers] == [start_timestamp, (start_timestamp + 160) & 0xffffffff]


@pytest.mark.asyncio
async def test_rtp_sends_nothing_until_nonempty_audio_is_queued():
    u, _ = ua()
    u.dialog = Dialog(("10.0.0.2", 5060), "call", "from", "to", 1, ("10.0.0.2", 4000))
    rtp = RTPProtocol(u); rtp.transport = Transport()

    await rtp.send_pcm24(b"")

    assert rtp.transport.sent == []
    assert rtp.send_queue.empty()
    assert rtp.sender_task is None


@pytest.mark.asyncio
async def test_rtp_queue_overflow_drops_oldest(caplog):
    u, _ = ua()
    u.dialog = Dialog(("10.0.0.2", 5060), "call", "from", "to", 1, ("10.0.0.2", 4000))
    rtp = RTPProtocol(u); rtp.transport = Transport()
    blocker = asyncio.Event()
    rtp.sender_task = asyncio.create_task(blocker.wait())
    packet_count = rtp.MAX_QUEUED_PACKETS + 2
    with caplog.at_level("WARNING", logger="app.sip"):
        await rtp.send_pcm24(b"\x00\x00" * (480 * packet_count))
    assert rtp.send_queue.qsize() == rtp.MAX_QUEUED_PACKETS
    assert "dropping oldest audio packet" in caplog.text
    await rtp.stop_sender()


@pytest.mark.asyncio
async def test_rtp_300_packet_burst_is_buffered_without_drops(monkeypatch, caplog):
    u, _ = ua()
    u.dialog = Dialog(("10.0.0.2", 5060), "call", "from", "to", 1, ("10.0.0.2", 4000))
    rtp = RTPProtocol(u); rtp.transport = Transport()
    blocker = asyncio.Event()
    rtp.sender_task = asyncio.create_task(blocker.wait())
    payload = b"".join(bytes([packet % 256]) * rtp.PACKET_SIZE for packet in range(300))
    monkeypatch.setattr("app.sip.pcm24_to_telephony", lambda pcm, codec, state, *, return_state: (pcm, state))

    with caplog.at_level("WARNING", logger="app.sip"):
        await rtp.send_pcm24(payload)

    assert rtp.send_queue.qsize() == 300
    assert list(rtp.send_queue._queue) == [bytes([packet % 256]) * rtp.PACKET_SIZE for packet in range(300)]
    assert "RTP send queue full" not in caplog.text
    await rtp.stop_sender()


@pytest.mark.asyncio
async def test_rtp_sender_uses_deadline_pacing(monkeypatch):
    u, _ = ua()
    u.dialog = Dialog(("10.0.0.2", 5060), "call", "from", "to", 1, ("10.0.0.2", 4000))
    rtp = RTPProtocol(u); rtp.transport = Transport()
    for packet in (b"a" * 160, b"b" * 160, b"c" * 160):
        rtp.send_queue.put_nowait(packet)
    clock = 10.0
    sleeps = []

    class Loop:
        def time(self):
            return clock

    async def sleep(delay):
        nonlocal clock
        sleeps.append(delay)
        clock += delay + 0.003
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr("app.sip.asyncio.get_running_loop", lambda: Loop())
    monkeypatch.setattr("app.sip.asyncio.sleep", sleep)
    with contextlib.suppress(asyncio.CancelledError):
        await rtp._send_packets()

    assert sleeps == pytest.approx([0.020, 0.017, 0.017])


@pytest.mark.asyncio
async def test_rtp_drain_waits_until_queue_is_empty():
    u, _ = ua()
    rtp = RTPProtocol(u)
    rtp.send_queue.put_nowait(b"packet")

    drain = asyncio.create_task(rtp.drain())
    await asyncio.sleep(0)
    assert not drain.done()
    rtp.send_queue.get_nowait()
    rtp.send_queue.task_done()
    await drain


@pytest.mark.asyncio
async def test_rtp_drain_returns_immediately_for_empty_queue():
    u, _ = ua()
    rtp = RTPProtocol(u)

    await rtp.drain()


@pytest.mark.asyncio
async def test_rtp_overflow_warning_is_rate_limited(monkeypatch, caplog):
    u, _ = ua()
    u.dialog = Dialog(("10.0.0.2", 5060), "call", "from", "to", 1, ("10.0.0.2", 4000))
    rtp = RTPProtocol(u); rtp.transport = Transport()
    blocker = asyncio.Event()
    rtp.sender_task = asyncio.create_task(blocker.wait())
    monkeypatch.setattr("app.sip.pcm24_to_telephony", lambda pcm, codec, state, *, return_state: (pcm, state))
    overflow = b"x" * (rtp.PACKET_SIZE * (rtp.MAX_QUEUED_PACKETS + 100))

    with caplog.at_level("WARNING", logger="app.sip"):
        await rtp.send_pcm24(overflow)

    warnings = [record for record in caplog.records if "RTP send queue full" in record.message]
    assert len(warnings) == 1
    await rtp.stop_sender()


@pytest.mark.asyncio
async def test_send_pcm24_preserves_ratecv_state(monkeypatch):
    u, _ = ua()
    u.dialog = Dialog(("10.0.0.2", 5060), "call", "from", "to", 1, ("10.0.0.2", 4000))
    rtp = RTPProtocol(u); rtp.transport = Transport()
    blocker = asyncio.Event()
    rtp.sender_task = asyncio.create_task(blocker.wait())
    states = []
    from app.realtime import pcm24_to_telephony as convert

    def recording_convert(data, codec, state, *, return_state):
        states.append(state)
        return convert(data, codec, state, return_state=return_state)

    monkeypatch.setattr("app.sip.pcm24_to_telephony", recording_convert)
    await rtp.send_pcm24(b"\x01\x00" * 481)
    await rtp.send_pcm24(b"\x02\x00" * 479)
    assert states[0] is None and states[1] is not None
    # The two calls contain exactly enough source samples for two RTP packets.
    assert rtp.send_queue.qsize() == 2
    await rtp.stop_sender()


def test_register_and_digest():
    u,_=ua(); msg=u.build_register().decode()
    assert msg.startswith("REGISTER sip:pbx SIP/2.0") and "Contact: <sip:200@10.0.0.3:5062>" in msg
    auth=digest_authorization('Digest realm="asterisk", nonce="abc"',"200","pw","REGISTER","sip:pbx")
    assert 'username="200"' in auth and 'response="' in auth


def test_ambient_pcm_mixing_and_disabled_behavior():
    u, _ = ua()
    u.ambient = AmbientConfig(enabled=True, volume=0.2)
    rtp = RTPProtocol(u)
    rtp._ambient_pcm = struct.pack("<160h", *([1000] * 160))
    voice = struct.pack("<160h", *([2000] * 160))
    mixed = rtp.mix_pcm16(voice)
    assert len(mixed) == len(voice) and mixed != voice
    u.ambient.volume = 0
    assert rtp.mix_pcm16(voice) == voice
    u.ambient.enabled, u.ambient.volume = False, 0.2
    assert rtp.mix_pcm16(voice) == voice


def test_ambient_pcm_mixing_without_voice_returns_one_packet():
    u, _ = ua()
    u.ambient = AmbientConfig(enabled=True, volume=0.2)
    rtp = RTPProtocol(u)
    rtp._ambient_pcm = struct.pack("<160h", *([1000] * 160))

    mixed = rtp.mix_pcm16(b"")

    assert len(mixed) == rtp.PACKET_SIZE * 2
    assert mixed != b"\x00" * (rtp.PACKET_SIZE * 2)


def test_missing_ambient_file_warns_without_crashing(caplog):
    u, _ = ua()
    u.ambient = AmbientConfig(enabled=True, file="missing.wav")
    with caplog.at_level("WARNING", logger="app.sip"):
        rtp = RTPProtocol(u)
    assert rtp._ambient_pcm == b""
    assert "Could not load ambient sound" in caplog.text


@pytest.mark.asyncio
async def test_ambient_sends_packets_without_voice():
    u, _ = ua()
    u.ambient = AmbientConfig(enabled=True, volume=0.2)
    u.dialog = Dialog(("10.0.0.2", 5060), "call", "from", "to", 1, ("10.0.0.2", 4000))
    rtp = RTPProtocol(u)
    rtp._ambient_pcm = struct.pack("<160h", *([1000] * 160))
    rtp.transport = Transport()
    rtp.start_sender()
    await asyncio.sleep(0.05)
    assert rtp.transport.sent
    assert all(len(packet) == 12 + rtp.PACKET_SIZE for packet, _ in rtp.transport.sent)
    await rtp.stop_sender()


@pytest.mark.asyncio
async def test_empty_pcm_queues_ambient_packet():
    u, _ = ua()
    u.ambient = AmbientConfig(enabled=True, volume=0.2)
    u.dialog = Dialog(("10.0.0.2", 5060), "call", "from", "to", 1, ("10.0.0.2", 4000))
    rtp = RTPProtocol(u)
    rtp._ambient_pcm = struct.pack("<160h", *([1000] * 160))
    rtp.transport = Transport()

    await rtp.send_pcm24(b"")

    assert rtp.send_queue.qsize() == 1
    await rtp.stop_sender()


@pytest.mark.asyncio
async def test_inbound_invite_answers_and_creates_session():
    u,calls=ua(); await u.handle_message(INVITE,("10.0.0.2",5060))
    assert calls.active.state == "active" and calls.active.number == "201"
    assert u.transport.sent[-1][0].startswith(b"SIP/2.0 200 OK")
    assert u.dialog.remote_rtp == ("10.0.0.2",4000)


@pytest.mark.asyncio
async def test_inbound_waits_for_realtime_before_answering():
    u, calls = ua()
    factory_started = asyncio.Event()
    release_factory = asyncio.Event()
    greeted = []

    class Realtime:
        async def greet(self):
            assert not any(wire.startswith(b"SIP/2.0 200 OK") for wire, _ in u.transport.sent)
            greeted.append(True)

    async def delayed_factory(_session):
        factory_started.set()
        await release_factory.wait()
        return Realtime()

    calls.realtime_factory = delayed_factory
    inbound = asyncio.create_task(u.handle_message(INVITE, ("10.0.0.2", 5060)))
    await factory_started.wait()
    assert u.transport.sent[-1][0].startswith(b"SIP/2.0 180 Ringing")
    assert not any(wire.startswith(b"SIP/2.0 200 OK") for wire, _ in u.transport.sent)

    release_factory.set()
    await inbound

    assert u.transport.sent[-1][0].startswith(b"SIP/2.0 200 OK")
    assert calls.active.state == "active"
    assert greeted == [True]


@pytest.mark.asyncio
async def test_inbound_retransmit_stays_ringing_while_realtime_starts():
    u, calls = ua()
    release_factory = asyncio.Event()

    async def delayed_factory(_session):
        await release_factory.wait()

    calls.realtime_factory = delayed_factory
    inbound = asyncio.create_task(u.handle_message(INVITE, ("10.0.0.2", 5060)))
    await asyncio.sleep(0)
    await u.handle_message(INVITE, ("10.0.0.2", 5060))
    assert u.transport.sent[-1][0].startswith(b"SIP/2.0 180 Ringing")
    assert not any(wire.startswith(b"SIP/2.0 486") for wire, _ in u.transport.sent)

    release_factory.set()
    await inbound


@pytest.mark.asyncio
async def test_inbound_realtime_timeout_still_answers(monkeypatch, caplog):
    u, calls = ua()

    async def never_ready(_session):
        await asyncio.Event().wait()

    calls.realtime_factory = never_ready
    monkeypatch.setattr("app.sip.INBOUND_REALTIME_TIMEOUT", 0.01)
    with caplog.at_level("WARNING", logger="app.sip"):
        await u.handle_message(INVITE, ("10.0.0.2", 5060))

    assert u.transport.sent[-1][0].startswith(b"SIP/2.0 200 OK")
    assert calls.active.state == "active"
    assert "answering inbound call anyway" in caplog.text


@pytest.mark.asyncio
async def test_options_responds_ok_with_transaction_headers_and_capabilities():
    u, _ = ua()
    options = (
        "OPTIONS sip:200@10.0.0.3:5062 SIP/2.0\r\n"
        "Via: SIP/2.0/UDP 10.0.0.2:5060;branch=z9hG4bKqualify\r\n"
        "From: <sip:asterisk@10.0.0.2>;tag=from-tag\r\n"
        "To: <sip:200@10.0.0.3>\r\n"
        "Call-ID: qualify-call\r\n"
        "CSeq: 42 OPTIONS\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()

    await u.handle_message(options, ("10.0.0.2", 5060))

    wire, addr = u.transport.sent[-1]
    assert addr == ("10.0.0.2", 5060)
    assert wire.startswith(b"SIP/2.0 200 OK\r\n")
    assert b"Via: SIP/2.0/UDP 10.0.0.2:5060;branch=z9hG4bKqualify\r\n" in wire
    assert b"From: <sip:asterisk@10.0.0.2>;tag=from-tag\r\n" in wire
    assert b"To: <sip:200@10.0.0.3>;tag=" in wire
    assert b"Call-ID: qualify-call\r\n" in wire
    assert b"CSeq: 42 OPTIONS\r\n" in wire
    assert b"Contact: <sip:200@10.0.0.3:5062>\r\n" in wire
    assert b"Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, REGISTER\r\n" in wire


@pytest.mark.asyncio
async def test_unknown_request_method_responds_not_implemented():
    u, _ = ua()
    request = (
        "MESSAGE sip:200@pbx SIP/2.0\r\n"
        "Via: SIP/2.0/UDP 10.0.0.2:5060;branch=z\r\n"
        "From: <sip:201@pbx>;tag=a\r\n"
        "To: <sip:200@pbx>\r\n"
        "Call-ID: unsupported-call\r\n"
        "CSeq: 3 MESSAGE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()

    await u.handle_message(request, ("10.0.0.2", 5060))

    wire, _ = u.transport.sent[-1]
    assert wire.startswith(b"SIP/2.0 501 Not Implemented\r\n")
    assert b"Call-ID: unsupported-call\r\n" in wire
    assert b"CSeq: 3 MESSAGE\r\n" in wire


@pytest.mark.asyncio
async def test_outbound_invite():
    u,calls=ua(); session=await u.invite("5551234")
    wire=u.transport.sent[-1][0]
    assert session.direction == "outbound" and wire.startswith(b"INVITE sip:5551234@pbx SIP/2.0")


@pytest.mark.asyncio
async def test_outbound_response_received_by_protocol_increments_rings():
    u, calls = ua(); session = await u.invite("5551234")
    u.datagram_received(ringing(session.call_id, u.dialog.cseq), u.server)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.rings == 1


@pytest.mark.asyncio
async def test_hostname_server_resolves_for_sendto():
    calls = CallManager(); calls.agent_name = "Agent"; calls.caller_id = "200"
    u = SIPUserAgent(SIPConfig(server_host="localhost", advertise_host="10.0.0.3"), calls)
    u.transport = Transport(); u.rtp_port = 40000; calls.sip = u

    await u.invite("5551234")

    wire, addr = u.transport.sent[-1]
    ipaddress.ip_address(addr[0])
    assert addr == u.server
    assert b"INVITE sip:5551234@localhost SIP/2.0" in wire
    assert b"From: \"Agent\" <sip:200@localhost>" in wire
    assert b"To: <sip:5551234@localhost>" in wire


@pytest.mark.asyncio
async def test_outbound_invite_without_transport_fails_cleanly():
    u,calls=ua(); u.transport=None
    with pytest.raises(RuntimeError, match="SIP transport not ready"):
        await u.invite("5551234")
    assert calls.active is None


def ringing(call_id, cseq):
    return f"SIP/2.0 180 Ringing\r\nCall-ID: {call_id}\r\nCSeq: {cseq} INVITE\r\nContent-Length: 0\r\n\r\n".encode()


@pytest.mark.asyncio
async def test_ring_limit_cancels_and_records_not_answered():
    u, calls = ua(); calls.max_rings = 2
    events = []
    calls.event_handler = lambda event, session, partial: events.append((event, session.outcome, session.rings))
    session = await u.invite("5551234"); cseq = u.dialog.cseq
    await u.handle_message(ringing(session.call_id, cseq), u.server)
    assert session.rings == 1 and calls.active is session
    await u.handle_message(ringing(session.call_id, cseq), u.server)
    wire = u.transport.sent[-1][0]
    assert wire.startswith(b"CANCEL sip:5551234@pbx SIP/2.0")
    assert calls.active is None and calls.history[-1].outcome == "not_answered"
    assert events[-1] == ("call.ended", "not_answered", 2)


@pytest.mark.asyncio
async def test_ring_timeout_cancels_once_before_ring_limit(monkeypatch):
    real_sleep = asyncio.sleep
    timeout_started = asyncio.Event()
    release_timeout = asyncio.Event()

    async def controlled_sleep(seconds):
        timeout_started.set()
        await release_timeout.wait()

    monkeypatch.setattr("app.sip.asyncio.sleep", controlled_sleep)
    u, calls = ua(); calls.max_rings = 10; calls.max_ring_seconds = 5
    session = await u.invite("5551234")
    await timeout_started.wait()
    release_timeout.set()
    await real_sleep(0)
    await real_sleep(0)

    cancels = [wire for wire, _ in u.transport.sent if wire.startswith(b"CANCEL ")]
    assert len(cancels) == 1
    assert calls.active is None and calls.history[-1].outcome == "not_answered"
    assert session.rings == 0
    await u._end_not_answered(session)
    assert len([wire for wire, _ in u.transport.sent if wire.startswith(b"CANCEL ")]) == 1


@pytest.mark.asyncio
async def test_answer_before_ring_limit_does_not_cancel():
    u, calls = ua(); calls.max_rings = 2
    session = await u.invite("5551234"); cseq = u.dialog.cseq
    await u.handle_message(ringing(session.call_id, cseq), u.server)
    answer = f"SIP/2.0 200 OK\r\nTo: <sip:5551234@pbx>;tag=b\r\nCall-ID: {session.call_id}\r\nCSeq: {cseq} INVITE\r\nContact: <sip:5551234@pbx>\r\nContent-Length: 0\r\n\r\n".encode()
    await u.handle_message(answer, u.server)
    await u.handle_message(ringing(session.call_id, cseq), u.server)
    assert session.state == "active" and session.rings == 1
    assert not any(wire.startswith(b"CANCEL ") for wire, _ in u.transport.sent)


@pytest.mark.asyncio
async def test_answer_failure_does_not_stop_later_sip_datagrams(caplog):
    u, calls = ua()

    async def broken_realtime(_session):
        raise ConnectionError("bad realtime credentials")

    calls.realtime_factory = broken_realtime
    u.datagram_received(INVITE, ("10.0.0.2", 5060))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert u.transport is not None
    assert "answering anyway" in caplog.text
    assert u.transport.sent[-1][0].startswith(b"SIP/2.0 200 OK")

    # A subsequent datagram is still dispatched on the same protocol/socket.
    bye = ("BYE sip:200@pbx SIP/2.0\r\nVia: SIP/2.0/UDP 10.0.0.2:5060\r\n"
           "From: <sip:201@pbx>;tag=a\r\nTo: <sip:200@pbx>;tag=b\r\n"
           "Call-ID: call1\r\nCSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n").encode()
    u.datagram_received(bye, ("10.0.0.2", 5060))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls.active is None
    assert u.transport.sent[-1][0].startswith(b"SIP/2.0 200 OK")


@pytest.mark.asyncio
async def test_register_refresh_continues_after_outbound_call_ends(monkeypatch):
    u, calls = ua(); session = await u.invite("5551234")
    await calls.hangup()
    assert calls.active is None
    assert any(wire.startswith(b"CANCEL ") for wire, _ in u.transport.sent)

    refresh_now = asyncio.Event()

    async def one_refresh(_seconds):
        if refresh_now.is_set():
            raise asyncio.CancelledError
        refresh_now.set()

    monkeypatch.setattr("app.sip.asyncio.sleep", one_refresh)
    with pytest.raises(asyncio.CancelledError):
        await u._refresh()
    registers = [wire for wire, _ in u.transport.sent if wire.startswith(b"REGISTER ")]
    assert len(registers) == 1


@pytest.mark.asyncio
async def test_register_challenge_is_reused_for_refresh():
    u, _ = ua()
    challenge = ('SIP/2.0 401 Unauthorized\r\nCSeq: 1 REGISTER\r\n'
                 'WWW-Authenticate: Digest realm="asterisk", nonce="abc"\r\n'
                 'Content-Length: 0\r\n\r\n').encode()
    await u.handle_message(challenge, u.server)
    first = u.transport.sent[-1][0]
    refresh = u._authenticated_register()
    assert b'Authorization: Digest username="200"' in first
    assert b'Authorization: Digest username="200"' in refresh
    assert f"Call-ID: {u.register_call_id}".encode() in first
    assert f"Call-ID: {u.register_call_id}".encode() in refresh


@pytest.mark.asyncio
async def test_register_rejection_is_logged(caplog):
    u, _ = ua()
    rejected = b"SIP/2.0 403 Forbidden\r\nCSeq: 1 REGISTER\r\nContent-Length: 0\r\n\r\n"
    with caplog.at_level("ERROR", logger="app.sip"):
        await u.handle_message(rejected, u.server)
    assert "REGISTER rejected" in caplog.text


@pytest.mark.asyncio
async def test_register_timeout_is_logged(caplog, monkeypatch):
    async def immediate_sleep(_seconds):
        return None

    monkeypatch.setattr("app.sip.asyncio.sleep", immediate_sleep)
    u, _ = ua()
    with caplog.at_level("WARNING", logger="app.sip"):
        await u._register_timeout(u.register_attempt)
    assert "REGISTER timed out" in caplog.text


@pytest.mark.asyncio
async def test_outbound_invite_digest_challenge_is_retried():
    u, _ = ua(); session = await u.invite("5551234")
    cseq = u.dialog.cseq
    challenge = (f'SIP/2.0 401 Unauthorized\r\nCall-ID: {session.call_id}\r\n'
                 f'CSeq: {cseq} INVITE\r\nWWW-Authenticate: Digest realm="asterisk", nonce="abc"\r\n'
                 'Content-Length: 0\r\n\r\n').encode()
    await u.handle_message(challenge, u.server)
    wire = u.transport.sent[-1][0]
    ack = u.transport.sent[-2][0]
    assert ack.startswith(b"ACK sip:5551234@pbx SIP/2.0")
    assert f"CSeq: {cseq} ACK".encode() in ack
    assert wire.startswith(b"INVITE sip:5551234@pbx SIP/2.0")
    assert b'Authorization: Digest username="200"' in wire
    assert f"CSeq: {cseq + 1} INVITE".encode() in wire
