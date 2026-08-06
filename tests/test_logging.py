import logging

import pytest

import app.main as main_module
from app.config import ConfigStore
from app.main import MemoryHandler, Runtime


def test_runtime_captures_info_logs(tmp_path):
    runtime = Runtime(ConfigStore(tmp_path / "config.json"), start_sip=False)
    root = logging.getLogger()

    assert root.level == logging.INFO
    handler = next(
        handler
        for handler in root.handlers
        if isinstance(handler, MemoryHandler) and handler.target is runtime.logs
    )
    assert handler.level == logging.INFO

    logging.getLogger("agent_sip.test").info("visible info record")
    assert any("visible info record" in entry for entry in runtime.logs)


@pytest.mark.asyncio
async def test_restart_preserves_connected_voice_status(tmp_path, monkeypatch):
    class Server:
        should_exit = False
        async def serve(self): pass

    monkeypatch.setattr(main_module.uvicorn, "Server", lambda config: Server())
    runtime = Runtime(ConfigStore(tmp_path / "config.json"), start_sip=False)
    runtime.config.voice.api_key = "key"
    runtime.voice_status.update(True)
    status = runtime.voice_status

    await runtime.restart(runtime.config)

    assert runtime.voice_status is status
    assert runtime.voice_status.configured is True
    assert runtime.voice_status.connected is True
