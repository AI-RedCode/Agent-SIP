import asyncio
import logging

import pytest

import app.calls as calls_module
from app.calls import GOODBYE_RE, CallManager
from app.config import DEFAULT_OUTBOUND_AGENT_INSTRUCTIONS, VoiceConfig
from app.realtime import RealtimeClient


class RT:
    def __init__(self): self.spoken=[]; self.steered=[]; self.stopped=False
    async def say(self, text): self.spoken.append(text)
    async def steer(self, text): self.steered.append(text)
    async def stop(self): self.stopped=True


class SIP:
    def __init__(self, calls): self.calls = calls
    async def invite(self, number): return await self.calls.start("out", "outbound", number)


@pytest.mark.asyncio
async def test_call_direction_selects_session_prompt():
    calls = CallManager()
    calls.default_prompt = "fallback"
    calls.inbound_prompt = "inbound instructions"
    calls.outbound_prompt = "outbound instructions"

    inbound = await calls.start("in", "inbound", "201")
    assert inbound.prompt == "inbound instructions"
    calls.active = None

    outbound = await calls.start("out", "outbound", "202")
    assert outbound.prompt == "outbound instructions"


@pytest.mark.asyncio
async def test_inbound_brief_is_appended_only_when_set():
    calls = CallManager()
    calls.inbound_prompt = "inbound instructions"
    calls.inbound_brief = "Messages pour Monsieur X ou Madame Y"

    session = await calls.start("in", "inbound", "201")
    assert session.prompt == (
        "inbound instructions\n\n# OBJECTIF DE CET APPEL (CALL BRIEF)\n"
        "Messages pour Monsieur X ou Madame Y"
    )
    calls.active = None
    calls.inbound_brief = ""
    session = await calls.start("in-2", "inbound", "202")
    assert session.prompt == "inbound instructions"


@pytest.mark.asyncio
async def test_make_call_injects_call_brief_into_outbound_prompt_and_session_update():
    calls = CallManager()
    calls.sip = SIP(calls)
    calls.outbound_prompt = "outbound instructions"
    calls.default_language = "hy"

    session = await calls.make_call("202", "Ask whether the order is ready")

    assert "outbound instructions" in session.prompt
    assert "Ask whether the order is ready" in session.prompt
    assert "Session language: hy. Respond in hy" in session.prompt
    assert "unless the CALL BRIEF explicitly specifies another language" in session.prompt
    update = RealtimeClient(VoiceConfig(), session, lambda _: None)._session_update()
    assert update["session"]["instructions"] == session.prompt
    assert "Ask whether the order is ready" in update["session"]["instructions"]


@pytest.mark.asyncio
async def test_make_call_with_brief_falls_back_to_default_prompt():
    calls = CallManager()
    calls.sip = SIP(calls)
    calls.default_prompt = "default instructions"

    session = await calls.make_call("202", "Confirm the delivery address")

    assert "default instructions" in session.prompt
    assert "Confirm the delivery address" in session.prompt


@pytest.mark.asyncio
async def test_data_collection_brief_includes_non_commitment_call_type_rule():
    calls = CallManager()
    calls.sip = SIP(calls)
    calls.outbound_prompt = DEFAULT_OUTBOUND_AGENT_INSTRUCTIONS

    session = await calls.make_call("202", "Ask for the person's name and weight")

    assert "# OBJECTIF DE CET APPEL (CALL BRIEF)\nAsk for the person's name and weight" in session.prompt
    assert "## Call Type" in session.prompt
    assert "DATA COLLECTION" in session.prompt
    assert "it is NOT a booking or" in session.prompt
    assert "je dois faire confirmer" in session.prompt
    assert "unless the CALL" in session.prompt
    assert "They never apply to information-collection calls" in session.prompt


@pytest.mark.asyncio
async def test_offer_proposed_by_brief_is_authorized():
    calls = CallManager()
    calls.sip = SIP(calls)
    calls.outbound_prompt = DEFAULT_OUTBOUND_AGENT_INSTRUCTIONS

    session = await calls.make_call(
        "202", "Offre : -20% la première semaine. Voulez-vous une table pour deux ?"
    )

    assert "that proposal is authorization" in session.prompt
    assert "Finalize it and do not say that confirmation is required" in session.prompt


@pytest.mark.asyncio
async def test_make_call_without_brief_appends_default_language():
    calls = CallManager()
    calls.sip = SIP(calls)
    calls.default_prompt = "default instructions"
    calls.outbound_prompt = "outbound instructions"

    session = await calls.make_call("202")

    assert session.prompt.startswith("outbound instructions\n\nSession language: fr. Respond in fr")


@pytest.mark.asyncio
async def test_transcript_and_lifecycle():
    calls = CallManager(); session = await calls.start("id", "inbound", "201")
    session.realtime = RT(); await calls.answered(session)
    session.add_transcript("caller", " Hello "); await calls.say("Hi")
    assert calls.transcript()[0]["text"] == "Hello"
    await calls.hangup(remote=True)
    assert calls.active is None and calls.history[-1].state == "ended" and session.realtime.stopped


@pytest.mark.asyncio
async def test_consecutive_agent_turn_guard_hangs_up_completed(caplog):
    calls = CallManager(); calls.max_agent_turns = 3
    session = await calls.start("id", "inbound", "201")
    session.state = "active"; session.realtime = RT()
    with caplog.at_level(logging.WARNING, logger="app.calls"):
        await calls.add_transcript(session, "agent", "Premier tour")
        await calls.add_transcript(session, "agent", "Deuxième tour")
        await calls.add_transcript(session, "agent", "Troisième tour")
    assert calls.active is None and session.outcome == "completed"
    assert session.realtime.stopped
    assert "auto-ending call: agent spoke too many consecutive turns" in caplog.text


@pytest.mark.asyncio
async def test_caller_turn_resets_consecutive_agent_turn_guard():
    calls = CallManager(); calls.max_agent_turns = 3
    session = await calls.start("id", "inbound", "201")
    await calls.add_transcript(session, "agent", "Un")
    await calls.add_transcript(session, "agent", "Deux")
    await calls.add_transcript(session, "caller", "Réponse")
    await calls.add_transcript(session, "agent", "Merci")
    assert calls.active is session and session.consecutive_agent_turns == 1


@pytest.mark.asyncio
async def test_duplicate_agent_lines_are_recorded_but_suppressed(caplog):
    calls = CallManager()
    session = await calls.start("id", "inbound", "201")
    emitted = []
    calls.event_handler = lambda event, *_: emitted.append(event)

    with caplog.at_level(logging.WARNING, logger="app.calls"):
        assert await calls.add_transcript(session, "agent", "D'accord, je l'enregistre.")
        assert not await calls.add_transcript(session, "agent", "  D'ACCORD je l'enregistre!  ")

    assert [message.text for message in session.transcript] == [
        "D'accord, je l'enregistre.", "D'ACCORD je l'enregistre!",
    ]
    assert emitted == ["transcript.final"]
    assert session.consecutive_agent_turns == 1
    assert "suppressed duplicate agent line" in caplog.text


@pytest.mark.asyncio
async def test_meaningfully_different_agent_line_is_not_suppressed():
    calls = CallManager()
    session = await calls.start("id", "inbound", "201")

    assert await calls.add_transcript(session, "agent", "D'accord, je l'enregistre.")
    assert await calls.add_transcript(session, "agent", "C'est noté. Au revoir.")
    assert session.consecutive_agent_turns == 2


@pytest.mark.asyncio
async def test_goodbye_then_noise_hangs_up_completed():
    calls = CallManager(); calls.end_grace_seconds = 0.01
    session = await calls.start("id", "inbound", "201")
    session.state = "active"; session.realtime = RT()
    await calls.add_transcript(session, "agent", "Merci, au revoir.")
    await calls.add_transcript(session, "caller", "bla bla")
    await asyncio.sleep(0.02)
    assert calls.active is None and session.outcome == "completed"


@pytest.mark.asyncio
async def test_goodbye_hangup_drains_rtp_before_bye():
    calls = CallManager(); calls.end_grace_seconds = 0
    session = await calls.start("id", "inbound", "201")
    session.state = "active"; session.realtime = RT()
    events = []

    class RTP:
        async def drain(self): events.append("drain")
        async def stop_sender(self): events.append("stop_sender")

    class RecordingSIP:
        rtp = RTP()
        async def bye(self, _session): events.append("bye")

    calls.sip = RecordingSIP()
    await calls.add_transcript(session, "agent", "Merci beaucoup ! Au revoir !")
    await session.end_grace_task

    assert events == ["drain", "bye", "stop_sender"]
    assert calls.active is None


@pytest.mark.asyncio
async def test_partial_goodbye_does_not_schedule_hangup():
    calls = CallManager()
    session = await calls.start("id", "inbound", "201")

    await calls.add_transcript(session, "agent", "Au revoir", partial=True)

    assert not session.goodbye_said
    assert session.end_grace_task is None


@pytest.mark.asyncio
async def test_meaningful_caller_turn_cancels_goodbye_hangup():
    calls = CallManager(); calls.end_grace_seconds = 0.01
    session = await calls.start("id", "inbound", "201")
    await calls.add_transcript(session, "agent", "Au revoir")
    await calls.add_transcript(session, "caller", "En fait j'aimerais aussi une table samedi")
    await asyncio.sleep(0.02)
    assert calls.active is session and not session.goodbye_said


@pytest.mark.parametrize("text", ["au revoir", "bonne journée", "je mets fin à l'appel", "terminé"])
def test_goodbye_regex(text):
    assert GOODBYE_RE.search(text)


@pytest.mark.asyncio
async def test_say_waits_for_realtime_session():
    calls = CallManager()
    session = await calls.start("id", "outbound", "100")
    session.state = "active"
    task = asyncio.create_task(calls.say("Question?"))
    await asyncio.sleep(0)

    realtime = RT()
    session.realtime = realtime
    session.realtime_ready.set()
    await task

    assert realtime.spoken == ["Question?"]


@pytest.mark.asyncio
async def test_steer_active_call_and_reject_without_one():
    calls = CallManager()
    with pytest.raises(RuntimeError, match="no active call"):
        await calls.steer("Be warmer")
    session = await calls.start("id", "outbound", "100")
    session.state = "active"
    session.realtime = RT()
    await calls.steer("Be warmer")
    assert session.realtime.steered == ["Be warmer"]


@pytest.mark.asyncio
async def test_say_realtime_timeout_warns_and_does_not_raise(monkeypatch, caplog):
    calls = CallManager()
    session = await calls.start("id", "outbound", "100")
    session.state = "active"
    monkeypatch.setattr(calls_module, "REALTIME_READY_TIMEOUT", 0.01)

    with caplog.at_level(logging.WARNING, logger="app.calls"):
        await calls.say("Question?")

    assert "dropping text" in caplog.text


@pytest.mark.asyncio
async def test_answered_signals_readiness_when_realtime_factory_fails():
    calls = CallManager()
    session = await calls.start("id", "inbound", "100")

    async def fail(_session):
        raise RuntimeError("connection failed")

    calls.realtime_factory = fail
    with pytest.raises(RuntimeError, match="connection failed"):
        await calls.answered(session)

    assert session.realtime_ready.is_set()
