from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging
from urllib.parse import urlencode
from typing import Awaitable, Callable

import websockets

from .calls import CLOSING, SAVING, normalize_agent_line
from .messages import MessageInput

log = logging.getLogger(__name__)
FINAL_CLOSING = "C'est noté. Au revoir."
FAILED_CLOSING = "Je ne peux pas enregistrer le message. Au revoir."
SAVE_MESSAGE_TOOL = {
    "type": "function",
    "name": "save_message",
    "description": "Save a caller-confirmed household message",
    "parameters": {
        "type": "object",
        "properties": {
            "recipient": {"type": "string", "enum": ["monsieur_mounier", "madame_astride"]},
            "caller_name": {"type": "string"},
            "message": {"type": "string"},
            "callback_number": {"type": ["string", "null"]},
            "language": {"type": "string", "enum": ["fr"]},
            "confirmed_by_caller": {"type": "boolean"},
        },
        "required": ["recipient", "caller_name", "message", "callback_number", "language", "confirmed_by_caller"],
    },
}
HANGUP_TOOL = {
    "type": "function", "name": "hangup_call", "description": "Hang up the active call",
    "parameters": {"type": "object", "properties": {}},
}


def telephony_to_pcm24(data: bytes, codec: str = "pcmu", state=None, *, return_state: bool = False):
    pcm = audioop.ulaw2lin(data, 2) if codec == "pcmu" else audioop.alaw2lin(data, 2)
    converted, state = audioop.ratecv(pcm, 2, 1, 8000, 24000, state)
    return (converted, state) if return_state else converted


def pcm24_to_telephony(data: bytes, codec: str = "pcmu", state=None, *, return_state: bool = False):
    pcm, state = audioop.ratecv(data, 2, 1, 24000, 8000, state)
    converted = audioop.lin2ulaw(pcm, 2) if codec == "pcmu" else audioop.lin2alaw(pcm, 2)
    return (converted, state) if return_state else converted


class RealtimeClient:
    def __init__(self, config, call, audio_sender: Callable[[bytes], Awaitable[None]], connector=None, status_callback=None):
        self.config, self.call, self.audio_sender = config, call, audio_sender
        self.connector = connector or websockets.connect
        self.status_callback = status_callback
        self.ws = None
        self.task: asyncio.Task | None = None
        self.session_ready: asyncio.Future | None = None
        self.steer_ready: asyncio.Future | None = None
        self.steer_lock = asyncio.Lock()
        self.greeted = False
        self.pending_agent_audio: list[bytes] = []
        self.agent_audio_allowed: bool | None = None

    def _uses_current_schema(self) -> bool:
        """GA Realtime models use the nested audio session shape."""
        return not self.config.model.startswith("gpt-4o-realtime-preview")

    def _session_update(self) -> dict:
        if not self._uses_current_schema():
            return {
                "type": "session.update",
                "session": {
                    "instructions": self.call.prompt,
                    "voice": self.config.voice,
                    "speed": self.config.speed,
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                    "input_audio_transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {"type": "server_vad"},
                    "tools": [SAVE_MESSAGE_TOOL, HANGUP_TOOL],
                },
            }

        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self.config.model,
                "instructions": self.call.prompt,
                "output_modalities": ["audio"],
                "tools": [SAVE_MESSAGE_TOOL, HANGUP_TOOL],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": {"type": "server_vad"},
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": self.config.voice,
                        "speed": self.config.speed,
                    },
                },
            },
        }

    async def start(self):
        separator = "&" if "?" in self.config.base_url else "?"
        url = f"{self.config.base_url}{separator}{urlencode({'model': self.config.model})}"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        if not self._uses_current_schema():
            headers["OpenAI-Beta"] = "realtime=v1"
        try:
            self.ws = await self.connector(url, additional_headers=headers)
            self.session_ready = asyncio.get_running_loop().create_future()
            self.task = asyncio.create_task(self._receive())
            await self.ws.send(json.dumps(self._session_update()))
            await self.session_ready
        except BaseException as exc:
            if self.status_callback:
                self.status_callback(False, str(exc))
            await self.stop()
            raise
        if self.status_callback:
            self.status_callback(True, None)
        return self

    async def send_audio(self, pcm24: bytes) -> None:
        if self.ws:
            try:
                await self.ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm24).decode()}))
            except (
                websockets.exceptions.ConnectionClosedOK,
                websockets.exceptions.ConnectionClosedError,
                websockets.exceptions.ConnectionClosed,
            ):
                return

    async def say(self, text: str) -> None:
        if not self.ws:
            raise RuntimeError("realtime websocket is not connected")
        modalities_key = "output_modalities" if self._uses_current_schema() else "modalities"
        await self._create_response({
            modalities_key: ["audio"],
            "instructions": f"Dites exactement ceci à l'appelant : {text}",
        })

    async def _create_response(self, response: dict, *, closing: bool = False) -> bool:
        """The sole response.create gate during message saving/closing."""
        if self.call.conversation_state == SAVING:
            log.warning("suppressed response.create while saving message")
            return False
        if self.call.conversation_state == CLOSING:
            if not closing or self.call.closing_response_created:
                log.warning("suppressed duplicate response.create during closing")
                return False
            self.call.closing_response_created = True
        await self.ws.send(json.dumps({"type": "response.create", "response": response}))
        return True

    async def steer(self, instructions: str | None = None, speed: float | None = None) -> None:
        if not self.ws:
            raise RuntimeError("realtime websocket is not connected")
        async with self.steer_lock:
            session = {}
            if instructions is not None:
                self.call.prompt = instructions
                session["instructions"] = instructions
            if speed is not None:
                speed = float(speed)
                if not 0.25 <= speed <= 4.0:
                    raise ValueError("speed must be between 0.25 and 4.0")
                if self._uses_current_schema():
                    session["audio"] = {"output": {"speed": speed}}
                else:
                    session["speed"] = speed
            update = {
                "type": "session.update",
                "session": session,
            }
            log.info("Sending Realtime steer session.update: %s", update)
            self.steer_ready = asyncio.get_running_loop().create_future()
            await self.ws.send(json.dumps(update))
            try:
                await asyncio.wait_for(self.steer_ready, timeout=5)
            except TimeoutError as exc:
                log.error("Timed out waiting for Realtime session.updated after steer")
                raise RuntimeError("realtime did not confirm the steering update") from exc
            finally:
                self.steer_ready = None
            if speed is not None:
                self.config.speed = speed

            if instructions is None:
                return
            modalities_key = "output_modalities" if self._uses_current_schema() else "modalities"
            response = {
                "type": "response.create",
                "response": {
                    modalities_key: ["audio"],
                    "instructions": (
                        f"{instructions}\n\n"
                        "Répondez maintenant à l'appelant en respectant immédiatement ces nouvelles instructions."
                    ),
                },
            }
            log.info("Sending Realtime steer response.create")
            await self._create_response(response["response"])

    async def greet(self) -> None:
        """Start the first model turn using the configured session instructions."""
        if not self.ws:
            raise RuntimeError("realtime websocket is not connected")
        if self.greeted:
            return
        self.greeted = True
        modalities_key = "output_modalities" if self._uses_current_schema() else "modalities"
        await self._create_response({modalities_key: ["audio"]})

    async def _handle_function_call(self, item: dict) -> None:
        name = item.get("name")
        if name == "hangup_call":
            if self.call.manager:
                await self.call.manager.hangup(outcome="completed")
            return
        if name != "save_message":
            return
        if self.call.conversation_state in {SAVING, CLOSING}:
            log.warning("suppressed duplicate save_message function call")
            return

        self.call.conversation_state = SAVING
        self.pending_agent_audio.clear()
        self.agent_audio_allowed = False
        try:
            arguments = json.loads(item.get("arguments") or "{}")
            store = getattr(self.call.manager, "message_store", None)
            if store is None:
                raise RuntimeError("message store is unavailable")
            result = store.save(MessageInput.model_validate(arguments))
            output = {"ok": True, "result": result}
            instructions = "Le message est enregistré. Dites exactement : « C'est noté. Au revoir. » Puis appelez hangup_call."
        except Exception as exc:
            log.warning("save_message failed: %s", exc)
            output = {"ok": False, "error": str(exc)}
            instructions = "Dites exactement : « Je ne peux pas enregistrer le message. Au revoir. » Puis appelez hangup_call."

        await self.ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": item.get("call_id"),
                "output": json.dumps(output, ensure_ascii=False),
            },
        }))
        self.call.conversation_state = CLOSING
        self.agent_audio_allowed = None
        modalities_key = "output_modalities" if self._uses_current_schema() else "modalities"
        await self._create_response({
            modalities_key: ["audio"],
            "instructions": instructions,
        }, closing=True)

    async def handle_event(self, event: dict) -> None:
        kind = event.get("type", "")
        log.debug("Realtime event: %s", kind)
        if kind == "session.updated":
            if self.session_ready and not self.session_ready.done():
                self.session_ready.set_result(None)
            elif self.steer_ready and not self.steer_ready.done():
                log.info("Realtime steer session.update accepted")
                self.steer_ready.set_result(None)
        elif kind in {"input_audio_transcription.completed", "conversation.item.input_audio_transcription.completed"}:
            if self.call.manager: await self.call.manager.add_transcript(self.call, "caller", event.get("transcript", ""))
            else: self.call.add_transcript("caller", event.get("transcript", ""))
        elif kind in {"response.audio_transcript.done", "response.output_audio_transcript.done"}:
            transcript = event.get("transcript", "")
            if self.call.conversation_state == SAVING:
                self.pending_agent_audio.clear()
                self.agent_audio_allowed = False
                return
            if (self.call.conversation_state == CLOSING
                    and normalize_agent_line(transcript) not in {
                        normalize_agent_line(FINAL_CLOSING), normalize_agent_line(FAILED_CLOSING)
                    }):
                log.warning("suppressed non-closing agent response during closing: %s", transcript)
                self.pending_agent_audio.clear()
                self.agent_audio_allowed = False
                return
            if self.call.manager:
                self.agent_audio_allowed = await self.call.manager.add_transcript(
                    self.call, "agent", transcript
                )
            else:
                self.call.add_transcript("agent", transcript)
                self.agent_audio_allowed = True
            if self.agent_audio_allowed:
                for audio in self.pending_agent_audio:
                    await self.audio_sender(audio)
            self.pending_agent_audio.clear()
        elif kind in {"input_audio_transcription.delta", "conversation.item.input_audio_transcription.delta", "response.audio_transcript.delta", "response.output_audio_transcript.delta"}:
            role = "agent" if kind.startswith("response.") else "caller"
            if self.call.manager: await self.call.manager.add_transcript(self.call, role, event.get("delta", ""), partial=True)
        elif kind in {"response.audio.delta", "response.output_audio.delta"}:
            if self.call.conversation_state == SAVING or self.agent_audio_allowed is False or (
                self.call.goodbye_said and self.agent_audio_allowed is not True
            ):
                return
            try:
                audio = base64.b64decode(event.get("delta", ""), validate=True)
                if self.agent_audio_allowed is True:
                    await self.audio_sender(audio)
                else:
                    self.pending_agent_audio.append(audio)
            except (ValueError, TypeError):
                log.warning("Ignoring invalid realtime audio payload")
        elif kind == "response.done":
            # Each response gets a fresh decision once its final transcript arrives.
            self.pending_agent_audio.clear()
            self.agent_audio_allowed = None
        elif kind == "response.output_item.done" and event.get("item", {}).get("type") == "function_call":
            await self._handle_function_call(event["item"])
        elif kind == "response.function_call_arguments.done":
            await self._handle_function_call(event)
        elif kind == "error":
            log.error("Realtime error: %s", event.get("error", event))
            error = event.get("error", event)
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            if self.session_ready and not self.session_ready.done():
                self.session_ready.set_exception(RuntimeError(message))
            if self.steer_ready and not self.steer_ready.done():
                self.steer_ready.set_exception(RuntimeError(f"realtime rejected steering update: {message}"))

    async def _receive(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    await self.handle_event(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    log.warning("Ignoring invalid realtime event")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Realtime connection ended: %s", exc)
        finally:
            if self.session_ready and not self.session_ready.done():
                self.session_ready.set_exception(ConnectionError("realtime connection ended before session was ready"))

    async def stop(self) -> None:
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
        if self.ws:
            await self.ws.close()


class RealtimeCallAdapter:
    """Adds agent instructions and codec conversion around RealtimeClient."""
    def __init__(self, client: RealtimeClient, prompt: str):
        self.client, self.prompt = client, prompt

    def __getattr__(self, name):
        return getattr(self.client, name)
