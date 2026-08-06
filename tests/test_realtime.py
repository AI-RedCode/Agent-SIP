import asyncio
import base64
import json
import logging

import pytest
from websockets.exceptions import ConnectionClosedOK

from app.calls import CLOSING, SAVING, CallManager, CallSession
from app.config import VoiceConfig
from app.messages import MessageStore
from app.realtime import FINAL_CLOSING, RealtimeClient, pcm24_to_telephony, telephony_to_pcm24
from app.sip import pcma_decode, pcma_encode, pcmu_decode, pcmu_encode


class FakeWebSocket:
    def __init__(self, events=None):
        self.sent = []
        self.events = iter(events or [{"type": "session.updated"}])

    async def send(self, message):
        self.sent.append(json.loads(message))

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return json.dumps(next(self.events))
        except StopIteration:
            raise StopAsyncIteration

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_send_audio_ignores_normal_websocket_close(caplog):
    class ClosedWebSocket:
        async def send(self, message):
            raise ConnectionClosedOK(None, None)

    client = RealtimeClient(VoiceConfig(), CallSession("x", "inbound", "1"), lambda _: None)
    client.ws = ClosedWebSocket()

    with caplog.at_level(logging.ERROR):
        await client.send_audio(b"audio")

    assert not caplog.records


@pytest.mark.asyncio
async def test_current_realtime_session_setup():
    ws = FakeWebSocket()
    connected = {}

    async def connector(url, **kwargs):
        connected.update(url=url, **kwargs)
        return ws

    config = VoiceConfig(api_key="sk-test", model="gpt-realtime-2.1", voice="alloy")
    call = CallSession("x", "inbound", "1")
    call.prompt = "Be concise"
    client = await RealtimeClient(config, call, lambda _: None, connector=connector).start()
    await client.task

    assert connected == {
        "url": "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1",
        "additional_headers": {"Authorization": "Bearer sk-test"},
    }
    assert ws.sent[0]["type"] == "session.update"
    assert ws.sent[0]["session"] == {
            "type": "realtime",
            "model": "gpt-realtime-2.1",
            "instructions": "Be concise",
            "output_modalities": ["audio"],
            "tools": ws.sent[0]["session"]["tools"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {"type": "server_vad"},
                },
                "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "alloy", "speed": 1.0},
            },
        }
    await client.say("Hello")
    assert ws.sent[-1]["response"] == {
        "output_modalities": ["audio"],
        "instructions": "Dites exactement ceci à l'appelant : Hello",
    }
    steer = asyncio.create_task(client.steer("Be warmer"))
    await asyncio.sleep(0)
    await client.handle_event({"type": "session.updated"})
    await steer
    assert call.prompt == "Be warmer"
    assert ws.sent[-2] == {
        "type": "session.update",
        "session": {"instructions": "Be warmer"},
    }
    assert ws.sent[-1] == {
        "type": "response.create",
        "response": {
            "output_modalities": ["audio"],
            "instructions": (
                "Be warmer\n\nRépondez maintenant à l'appelant en respectant "
                "immédiatement ces nouvelles instructions."
            ),
        },
    }
    await client.greet()
    assert ws.sent[-1] == {
        "type": "response.create",
        "response": {
            "output_modalities": ["audio"],
        },
    }


@pytest.mark.asyncio
async def test_realtime_connection_status_callback_success_and_failure():
    statuses = []

    async def connector(url, **kwargs):
        return FakeWebSocket()

    config = VoiceConfig(api_key="sk-test")
    call = CallSession("x", "inbound", "1")
    client = await RealtimeClient(
        config, call, lambda _: None, connector=connector,
        status_callback=lambda connected, error: statuses.append((connected, error)),
    ).start()
    await client.task
    assert statuses == [(True, None)]

    async def failed_connector(url, **kwargs):
        raise RuntimeError("invalid_api_key")

    with pytest.raises(RuntimeError, match="invalid_api_key"):
        await RealtimeClient(
            config, call, lambda _: None, connector=failed_connector,
            status_callback=lambda connected, error: statuses.append((connected, error)),
        ).start()
    assert statuses[-1] == (False, "invalid_api_key")


@pytest.mark.asyncio
async def test_legacy_realtime_session_setup():
    ws = FakeWebSocket()
    connected = {}

    async def connector(url, **kwargs):
        connected.update(url=url, **kwargs)
        return ws

    config = VoiceConfig(api_key="sk-test", model="gpt-4o-realtime-preview")
    call = CallSession("x", "inbound", "1")
    call.prompt = "Be concise"
    client = await RealtimeClient(config, call, lambda _: None, connector=connector).start()
    await client.task

    assert connected["additional_headers"]["OpenAI-Beta"] == "realtime=v1"
    assert ws.sent[0]["session"]["input_audio_format"] == "pcm16"
    assert ws.sent[0]["session"]["speed"] == 1.0
    assert "audio" not in ws.sent[0]["session"]


@pytest.mark.asyncio
async def test_steer_can_update_speed_and_instruction_only_leaves_it_unchanged():
    ws = FakeWebSocket()
    config = VoiceConfig(speed=1.0)
    call = CallSession("x", "inbound", "1")
    client = RealtimeClient(config, call, lambda _: None)
    client.ws = ws

    steer = asyncio.create_task(client.steer("Speak briskly", 1.25))
    await asyncio.sleep(0)
    await client.handle_event({"type": "session.updated"})
    await steer
    assert ws.sent[0] == {
        "type": "session.update",
        "session": {
            "instructions": "Speak briskly",
            "audio": {"output": {"speed": 1.25}},
        },
    }
    assert config.speed == 1.25

    steer = asyncio.create_task(client.steer("Be warmer"))
    await asyncio.sleep(0)
    await client.handle_event({"type": "session.updated"})
    await steer
    assert ws.sent[-2]["session"] == {"instructions": "Be warmer"}
    assert config.speed == 1.25


@pytest.mark.asyncio
async def test_realtime_events_and_invalid_audio():
    sent=[]; call=CallSession("x", "inbound", "1")
    async def audio(data): sent.append(data)
    client=RealtimeClient(VoiceConfig(), call, audio)
    await client.handle_event({"type":"input_audio_transcription.completed","transcript":"hello"})
    await client.handle_event({"type":"response.audio_transcript.done","transcript":"hi"})
    await client.handle_event({"type":"response.audio.delta","delta":base64.b64encode(b"abc").decode()})
    await client.handle_event({"type":"response.audio.delta","delta":"%%%"})
    assert [m.role for m in call.transcript] == ["caller", "agent"] and sent == [b"abc"]


@pytest.mark.asyncio
async def test_duplicate_agent_audio_is_buffered_and_suppressed():
    sent = []
    manager = CallManager()
    call = await manager.start("x", "inbound", "1")
    client = RealtimeClient(VoiceConfig(), call, lambda data: _append(sent, data))
    audio = base64.b64encode(b"voice").decode()

    await client.handle_event({"type": "response.audio.delta", "delta": audio})
    await client.handle_event({
        "type": "response.audio_transcript.done",
        "transcript": "D'accord, je l'enregistre.",
    })
    await client.handle_event({"type": "response.done"})
    await client.handle_event({"type": "response.audio.delta", "delta": audio})
    await client.handle_event({
        "type": "response.audio_transcript.done",
        "transcript": "D'ACCORD je l'enregistre!",
    })

    assert sent == [b"voice"]


@pytest.mark.asyncio
async def test_save_message_sends_one_output_and_one_closing_response(tmp_path):
    ws = FakeWebSocket()
    manager = CallManager()
    manager.message_store = MessageStore(tmp_path / "messages.json")
    call = await manager.start("x", "inbound", "1")
    client = RealtimeClient(VoiceConfig(), call, lambda _: None)
    client.ws = ws
    item = {
        "type": "function_call", "name": "save_message", "call_id": "call-1",
        "arguments": json.dumps({
            "recipient": "monsieur_mounier", "caller_name": "Jean",
            "message": "Rappelez-moi", "callback_number": None,
            "language": "fr", "confirmed_by_caller": True,
        }),
    }

    await client.handle_event({"type": "response.output_item.done", "item": item})
    await client.handle_event({"type": "response.output_item.done", "item": item})

    assert [message["type"] for message in ws.sent].count("conversation.item.create") == 1
    assert [message["type"] for message in ws.sent].count("response.create") == 1
    assert ws.sent[-1]["response"]["instructions"] == (
        "Le message est enregistré. Dites exactement : « C'est noté. Au revoir. » "
        "Puis appelez hangup_call."
    )
    assert call.conversation_state == CLOSING


@pytest.mark.asyncio
async def test_saving_and_closing_gate_agent_audio_and_duplicate_responses(caplog):
    sent = []
    call = CallSession("x", "inbound", "1")
    client = RealtimeClient(VoiceConfig(), call, lambda data: _append(sent, data))
    client.ws = FakeWebSocket()
    audio = base64.b64encode(b"voice").decode()

    call.conversation_state = SAVING
    await client.handle_event({"type": "response.audio.delta", "delta": audio})
    await client.handle_event({"type": "response.audio_transcript.done", "transcript": "Un instant."})
    assert sent == []

    call.conversation_state = CLOSING
    await client.handle_event({"type": "response.audio.delta", "delta": audio})
    await client.handle_event({"type": "response.audio_transcript.done", "transcript": "Encore un instant."})
    await client.handle_event({"type": "response.done"})
    await client.handle_event({"type": "response.audio.delta", "delta": audio})
    await client.handle_event({"type": "response.audio_transcript.done", "transcript": FINAL_CLOSING})
    assert sent == [b"voice"]

    call.closing_response_created = False
    assert await client._create_response({"output_modalities": ["audio"]}, closing=True)
    assert not await client._create_response({"output_modalities": ["audio"]}, closing=True)
    assert len(client.ws.sent) == 1


async def _append(items, item):
    items.append(item)


@pytest.mark.asyncio
async def test_current_input_transcription_event_adds_caller_message():
    call = CallSession("x", "inbound", "1")
    client = RealtimeClient(VoiceConfig(), call, lambda _: None)
    await client.handle_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "Bonjour de la part de l'appelant",
    })
    assert [(message.role, message.text) for message in call.transcript] == [
        ("caller", "Bonjour de la part de l'appelant")
    ]


@pytest.mark.asyncio
async def test_greet_is_sent_only_once():
    ws = FakeWebSocket()
    client = RealtimeClient(VoiceConfig(), CallSession("x", "inbound", "1"), lambda _: None)
    client.ws = ws
    await client.greet()
    await client.greet()
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_steer_logs_and_surfaces_session_update_error(caplog):
    ws = FakeWebSocket()
    client = RealtimeClient(VoiceConfig(), CallSession("x", "inbound", "1"), lambda _: None)
    client.ws = ws

    with caplog.at_level(logging.ERROR):
        steer = asyncio.create_task(client.steer("Be a melancholic poet"))
        await asyncio.sleep(0)
        await client.handle_event({
            "type": "error",
            "error": {"message": "Invalid session update"},
        })
        with pytest.raises(RuntimeError, match="rejected steering update"):
            await steer

    assert "Invalid session update" in caplog.text
    assert [message["type"] for message in ws.sent] == ["session.update"]


def test_codecs_and_resample():
    pcm=(b"\x00\x00\x10\x00\xf0\xff")*160
    for enc,dec in ((pcmu_encode,pcmu_decode),(pcma_encode,pcma_decode)):
        assert len(dec(enc(pcm))) == len(pcm)
    assert len(telephony_to_pcm24(pcmu_encode(pcm), "pcmu")) > len(pcm)
    assert pcm24_to_telephony(telephony_to_pcm24(pcmu_encode(pcm)))
