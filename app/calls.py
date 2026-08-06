from __future__ import annotations

import asyncio
import inspect
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable


log = logging.getLogger(__name__)
REALTIME_READY_TIMEOUT = 10
GOODBYE_RE = re.compile(r"\bau revoir\b|\bbonne journ[ée]e\b|\bje mets fin\b|\btermin[ée]e?\b", re.I)
COLLECTING = "COLLECTING"
AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
SAVING = "SAVING"
CLOSING = "CLOSING"
ENDED = "ENDED"


class SIPTransportNotReadyError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_agent_line(text: str) -> str:
    without_punctuation = "".join(
        character for character in text.casefold()
        if not unicodedata.category(character).startswith("P")
    )
    return " ".join(without_punctuation.split())


@dataclass
class TranscriptMessage:
    role: str
    text: str
    timestamp: str = field(default_factory=now)


@dataclass
class CallSession:
    call_id: str
    direction: str
    number: str
    state: str = "ringing"
    started_at: str = field(default_factory=now)
    answered_at: str | None = None
    ended_at: str | None = None
    rings: int = 0
    outcome: str | None = None
    transcript: list[TranscriptMessage] = field(default_factory=list)
    realtime: object | None = field(default=None, repr=False)
    realtime_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    local_number: str = ""
    manager: object | None = field(default=None, repr=False)
    ring_timeout_task: asyncio.Task | None = field(default=None, repr=False)
    ending: bool = field(default=False, repr=False)
    prompt: str = field(default="", repr=False)
    consecutive_agent_turns: int = field(default=0, repr=False)
    goodbye_said: bool = field(default=False, repr=False)
    end_grace_task: asyncio.Task | None = field(default=None, repr=False)
    last_agent_line: str = field(default="", repr=False)
    conversation_state: str = field(default=COLLECTING, repr=False)
    closing_response_created: bool = field(default=False, repr=False)

    def add_transcript(self, role: str, text: str) -> None:
        text = text.strip()
        if text:
            self.transcript.append(TranscriptMessage(role, text))

    def public(self) -> dict:
        value = {k: getattr(self, k) for k in ("call_id", "direction", "number", "state", "started_at", "answered_at", "ended_at", "outcome", "rings")}
        value["duration"] = self.duration
        return value

    @property
    def duration(self) -> float:
        end = datetime.fromisoformat(self.ended_at) if self.ended_at else datetime.now(timezone.utc)
        start = datetime.fromisoformat(self.answered_at or self.started_at)
        return max(0.0, (end - start).total_seconds())


class CallManager:
    def __init__(self):
        self.active: CallSession | None = None
        self.history: list[CallSession] = []
        self.sip = None
        self.realtime_factory: Callable[[CallSession], Awaitable[object]] | None = None
        self._lock = asyncio.Lock()
        self.event_handler: Callable | None = None
        self.agent_name = "Agent"
        self.default_language = "fr"
        self.caller_id = "200"
        self.max_rings = 6
        self.max_ring_seconds = 30
        self.max_agent_turns = 3
        self.end_grace_seconds = 4.0
        self.default_prompt = ""
        self.inbound_prompt = ""
        self.inbound_brief = ""
        self.outbound_prompt = ""

    async def start(self, call_id: str, direction: str, number: str) -> CallSession:
        async with self._lock:
            if self.active:
                raise RuntimeError("a call is already active")
            prompt = (
                self.inbound_prompt if direction == "inbound" else self.outbound_prompt
            ) or self.default_prompt
            if direction == "inbound" and self.inbound_brief:
                brief_section = f"# OBJECTIF DE CET APPEL (CALL BRIEF)\n{self.inbound_brief}"
                prompt = f"{prompt}\n\n{brief_section}" if prompt else brief_section
            self.active = CallSession(
                call_id, direction, number, local_number=self.caller_id,
                manager=self, prompt=prompt,
            )
            await self.emit("call.started", self.active)
            return self.active

    async def answered(self, session: CallSession) -> None:
        self._cancel_ring_timeout(session)
        session.state, session.answered_at, session.outcome = "active", now(), "answered"
        if not session.realtime_ready.is_set():
            await self.prepare_realtime(session)

    async def prepare_realtime(self, session: CallSession) -> object | None:
        """Connect a call's voice backend without changing its SIP state."""
        try:
            if self.realtime_factory:
                session.realtime = await self.realtime_factory(session)
            return session.realtime
        finally:
            session.realtime_ready.set()

    async def emit(self, event: str, session: CallSession, partial: bool = False) -> None:
        if self.event_handler:
            result = self.event_handler(event, session, partial)
            if inspect.isawaitable(result):
                await result

    async def add_transcript(self, session: CallSession, role: str, text: str, partial: bool = False) -> bool:
        if partial:
            if text.strip():
                await self.emit("transcript.partial", session, True)
            return False
        if not text.strip():
            return False
        session.add_transcript(role, text)
        if role == "agent":
            normalized = normalize_agent_line(text)
            if normalized and normalized == session.last_agent_line:
                log.warning("suppressed duplicate agent line: %s", text.strip())
                return False
            session.last_agent_line = normalized
        if role == "caller":
            session.consecutive_agent_turns = 0
            # Ignore short VAD/transcription noise; a substantive correction or
            # new request is allowed to keep the call alive.
            if session.goodbye_said and len(re.findall(r"\b\w+\b", text, re.UNICODE)) >= 4:
                session.goodbye_said = False
                self._cancel_end_grace(session)
        elif role == "agent":
            if normalize_agent_line("C'est correct") in normalize_agent_line(text):
                session.conversation_state = AWAITING_CONFIRMATION
            session.consecutive_agent_turns += 1
            if session.goodbye_said:
                await self.hangup(outcome="completed")
                return True
            if GOODBYE_RE.search(text):
                session.goodbye_said = True
                session.end_grace_task = asyncio.create_task(self._end_after_grace(session))
        await self.emit("transcript.final", session)
        if (role == "agent" and session.consecutive_agent_turns >= self.max_agent_turns
                and self.active is session and not session.ending):
            log.warning("auto-ending call: agent spoke too many consecutive turns")
            await self.hangup(outcome="completed")
        return True

    async def make_call(self, number: str, call_brief: str | None = None) -> CallSession:
        if not self.sip:
            raise RuntimeError("SIP service is unavailable")
        session = await self.sip.invite(number)
        language_instruction = (
            f"Session language: {self.default_language}. Respond in {self.default_language} "
            "unless the CALL BRIEF explicitly specifies another language."
        )
        base_prompt = session.prompt or self.outbound_prompt or self.default_prompt
        session.prompt = f"{base_prompt}\n\n{language_instruction}" if base_prompt else language_instruction
        if call_brief:
            brief_section = f"# OBJECTIF DE CET APPEL (CALL BRIEF)\n{call_brief}"
            session.prompt = f"{session.prompt}\n\n{brief_section}"

            async def later():
                for _ in range(100):
                    if session.state == "active":
                        await self.say(call_brief)
                        return
                    await asyncio.sleep(.1)
            asyncio.create_task(later())
        return session

    async def say(self, text: str) -> None:
        session = self.active
        if not session or session.state != "active":
            raise RuntimeError("no active call")
        if not session.realtime:
            try:
                await asyncio.wait_for(session.realtime_ready.wait(), REALTIME_READY_TIMEOUT)
            except TimeoutError:
                log.warning("Realtime session was not ready within %ss; dropping text", REALTIME_READY_TIMEOUT)
                return
        if not session.realtime:
            log.warning("Realtime session is unavailable; dropping text")
            return
        await session.realtime.say(text)

    async def steer(self, text: str | None = None, speed: float | None = None) -> None:
        session = self.active
        if not session or session.state != "active":
            raise RuntimeError("no active call")
        if not session.realtime:
            try:
                await asyncio.wait_for(session.realtime_ready.wait(), REALTIME_READY_TIMEOUT)
            except TimeoutError:
                log.warning("Realtime session was not ready within %ss; dropping instructions", REALTIME_READY_TIMEOUT)
                return
        if not session.realtime:
            log.warning("Realtime session is unavailable; dropping instructions")
            return
        if speed is None:
            await session.realtime.steer(text)
        else:
            await session.realtime.steer(text, speed)

    async def hangup(self, remote: bool = False, outcome: str | None = None) -> None:
        session = self.active
        if not session:
            return
        session.ending = True
        self._cancel_ring_timeout(session)
        self._cancel_end_grace(session)
        if not remote and self.sip:
            rtp = getattr(self.sip, "rtp", None)
            if rtp:
                await rtp.drain()
            if session.state == "ringing" and session.direction == "outbound":
                await self.sip.cancel(session)
            else:
                await self.sip.bye(session)
        if session.realtime:
            await session.realtime.stop()
        rtp = getattr(self.sip, "rtp", None)
        if rtp:
            await rtp.stop_sender()
        session.state, session.ended_at = "ended", now()
        session.conversation_state = ENDED
        session.outcome = outcome or session.outcome or "failed"
        self.history.append(session)
        self.active = None
        await self.emit("call.ended", session)

    @staticmethod
    def _cancel_ring_timeout(session: CallSession) -> None:
        task = session.ring_timeout_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        session.ring_timeout_task = None

    async def _end_after_grace(self, session: CallSession) -> None:
        try:
            await asyncio.sleep(self.end_grace_seconds)
            if self.active is session and session.goodbye_said and not session.ending:
                await self.hangup(outcome="completed")
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _cancel_end_grace(session: CallSession) -> None:
        task = session.end_grace_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        session.end_grace_task = None

    def transcript(self) -> list[dict]:
        session = self.active or (self.history[-1] if self.history else None)
        return [vars(message) for message in session.transcript] if session else []
