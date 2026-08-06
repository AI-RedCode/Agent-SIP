from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import uvicorn

from .calls import CallManager
from .config import ConfigStore
from .mcp import create_mcp_app
from .messages import MessageStore
from .realtime import RealtimeClient
from .sip import SIPUserAgent
from .web import create_app
from .webhook import WebhookNotifier


class MemoryHandler(logging.Handler):
    def __init__(self, target): super().__init__(level=logging.INFO); self.target = target
    def emit(self, record): self.target.append(self.format(record))


@dataclass
class VoiceStatus:
    configured: bool = False
    connected: bool = False
    last_error: str | None = None
    last_check: str | None = None

    def update(self, connected: bool, error: str | None = None) -> None:
        self.connected = connected
        self.last_error = error
        self.last_check = datetime.now(timezone.utc).isoformat()

    def public(self) -> dict:
        return asdict(self)


class Runtime:
    def __init__(self, store=None, start_sip=True):
        self.store = store or ConfigStore()
        self.config = self.store.load()
        self.voice_status = VoiceStatus(configured=bool(self.config.voice.api_key))
        self.message_store = MessageStore(self.store.path.parent / "messages.json")
        self.calls = CallManager()
        self.sip = None
        self.start_sip = start_sip
        self.logs = deque(maxlen=200)
        self.mcp_server = None
        self.mcp_task = None
        self.webhook = None
        handler = MemoryHandler(self.logs); handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)

    async def startup(self): await self.restart(self.config)

    async def restart(self, config):
        if self.sip: await self.sip.stop()
        await self._stop_mcp()
        self.config = config
        if hasattr(self, "voice_status"):
            self.voice_status.configured = bool(config.voice.api_key)
        else:
            self.voice_status = VoiceStatus(configured=bool(config.voice.api_key))
        self.calls.agent_name, self.calls.caller_id, self.calls.max_rings, self.calls.max_ring_seconds = config.agent.name, config.agent.caller_id, config.agent.max_rings, config.agent.max_ring_seconds
        self.calls.default_language = config.agent.default_language
        self.calls.max_agent_turns = config.agent.max_agent_turns
        self.calls.end_grace_seconds = config.agent.end_grace_seconds
        self.calls.default_prompt = config.agent.default_prompt
        self.calls.inbound_prompt = config.agent.inbound_prompt
        self.calls.inbound_brief = config.agent.inbound_brief
        self.calls.outbound_prompt = config.agent.outbound_prompt
        self.calls.message_store = self.message_store
        mcp_url = f"http://{config.mcp.host}:{config.mcp.port}"
        self.webhook = WebhookNotifier(config.webhook, config.agent.name, mcp_url)
        self.calls.event_handler = self.webhook.dispatch
        uv_config = uvicorn.Config(create_mcp_app(self), host=config.mcp.host, port=config.mcp.port, log_level="info")
        self.mcp_server = uvicorn.Server(uv_config)
        self.mcp_task = asyncio.create_task(self.mcp_server.serve())
        if not self.start_sip: return
        self.sip = SIPUserAgent(config.sip, self.calls, config.ambient); self.calls.sip = self.sip
        async def realtime_factory(call):
            client = RealtimeClient(config.voice, call, self.sip.rtp.send_pcm24, status_callback=self._set_voice_status)
            return await client.start()
        self.calls.realtime_factory = realtime_factory
        try: await self.sip.start()
        except OSError as exc: logging.getLogger(__name__).error("SIP startup failed: %s", exc)

    def _set_voice_status(self, connected: bool, error: str | None = None) -> None:
        self.voice_status.update(connected, error)

    async def shutdown(self):
        if self.calls.active: await self.calls.hangup()
        if self.sip: await self.sip.stop()
        await self._stop_mcp()

    async def _stop_mcp(self):
        if self.mcp_server:
            self.mcp_server.should_exit = True
        if self.mcp_task:
            await self.mcp_task
        self.mcp_server = self.mcp_task = None


runtime = Runtime()
app = create_app(runtime)

@app.on_event("startup")
async def _startup(): await runtime.startup()

@app.on_event("shutdown")
async def _shutdown(): await runtime.shutdown()

def cli():
    uvicorn.run("app.main:app", host="0.0.0.0", port=8090, log_level="info")

if __name__ == "__main__": cli()
