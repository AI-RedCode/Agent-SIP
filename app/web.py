from __future__ import annotations

import hashlib
import hmac
import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .calls import SIPTransportNotReadyError
from .config import AppConfig
from .messages import MessageInput, runtime_message_store


log = logging.getLogger(__name__)
AMBIENT_DIR = Path(__file__).parent.parent / "assets" / "ambient"


class CallBody(BaseModel):
    number: str
    call_brief: str | None = None
    text: str | None = None


class SayBody(BaseModel):
    text: str


class SteerBody(BaseModel):
    instructions: str | None = None
    speed: float | None = None


class LoginBody(BaseModel):
    username: str
    password: str


COOKIE_NAME = "agent_sip_session"
SESSION_SECONDS = 60 * 60 * 24 * 7


def _secret(config) -> bytes:
    return hashlib.sha256((config.web.password + config.web.username).encode()).digest()


def _session_token(config, expires: int) -> str:
    message = f"{config.web.username}:{expires}"
    signature = hmac.new(_secret(config), message.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def _authenticated(request: Request, config) -> bool:
    if not config.web.enabled:
        return True
    try:
        expires_text, signature = request.cookies.get(COOKIE_NAME, "").split(".", 1)
        expires = int(expires_text)
    except (ValueError, TypeError):
        return False
    expected = _session_token(config, expires).split(".", 1)[1]
    return expires >= int(time.time()) and hmac.compare_digest(signature, expected)


def create_app(runtime) -> FastAPI:
    app = FastAPI(title="Agent-SIP", version="0.1.0")
    app.state.runtime = runtime

    @app.middleware("http")
    async def require_web_auth(request: Request, call_next):
        if request.url.path in {"/login", "/api/login"} or _authenticated(request, runtime.config):
            return await call_next(request)
        if request.url.path == "/":
            return RedirectResponse("/login", status_code=303)
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})

    @app.get("/login")
    async def login_page():
        return FileResponse(Path(__file__).parent.parent / "static" / "login.html")

    @app.post("/api/login")
    async def login(body: LoginBody):
        config = runtime.config
        valid = hmac.compare_digest(body.username.encode(), config.web.username.encode()) and hmac.compare_digest(body.password.encode(), config.web.password.encode())
        if not valid:
            raise HTTPException(401, "Invalid username or password")
        expires = int(time.time()) + SESSION_SECONDS
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE_NAME, _session_token(config, expires), max_age=SESSION_SECONDS, httponly=True, samesite="strict")
        return response

    @app.post("/api/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME)
        return response

    @app.get("/")
    async def index():
        return FileResponse(Path(__file__).parent.parent / "static" / "index.html")

    @app.get("/api/status")
    async def status():
        active = runtime.calls.active
        recent = active or (runtime.calls.history[-1] if runtime.calls.history else None)
        voice_status = getattr(runtime, "voice_status", None)
        voice = voice_status.public() if voice_status else {"configured": bool(runtime.config.voice.api_key), "connected": False, "last_error": None, "last_check": None}
        return {"sip_registered": runtime.sip.registered if runtime.sip else False, "voice": voice, "call_state": recent.state if recent else "idle", "active_call": active.public() if active else None, "last_call": recent.public() if recent else None, "agent": {"name": runtime.config.agent.name, "caller_id": runtime.config.agent.caller_id}, "logs": list(runtime.logs)[-80:]}

    @app.get("/api/config")
    async def get_config(): return runtime.config.to_dict()

    @app.put("/api/config")
    async def put_config(request: Request):
        try:
            data = await request.json()
            current = runtime.config.to_dict()
            for section in ("sip", "voice", "agent", "mcp", "webhook", "ambient", "web"):
                incoming = data.get(section, {})
                for key, value in incoming.items():
                    if value != "********": current[section][key] = value
            cfg = AppConfig.from_dict(current)
            runtime.store.save(cfg)
            await runtime.restart(cfg)
            return {"ok": True}
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/ambient/files")
    async def ambient_files():
        return {"files": sorted(path.name for path in AMBIENT_DIR.glob("*.wav") if path.is_file())}

    @app.post("/api/call")
    async def call(body: CallBody):
        if not body.number.strip(): raise HTTPException(422, "number is required")
        try:
            call_brief = body.call_brief if body.call_brief is not None else body.text
            return (await runtime.calls.make_call(body.number.strip(), call_brief)).public()
        except SIPTransportNotReadyError as exc:
            log.warning("Outbound call rejected: %s", exc)
            return JSONResponse(status_code=503, content={"ok": False, "error": str(exc)})
        except RuntimeError as exc: raise HTTPException(409, str(exc)) from exc

    @app.post("/api/hangup")
    async def hangup(): await runtime.calls.hangup(); return {"ok": True}

    @app.post("/api/say")
    async def say(body: SayBody):
        if not body.text.strip(): raise HTTPException(422, "text is required")
        try: await runtime.calls.say(body.text); return {"ok": True}
        except RuntimeError as exc: raise HTTPException(409, str(exc)) from exc

    @app.post("/api/steer")
    async def steer(body: SteerBody):
        instructions = body.instructions.strip() if body.instructions else None
        if instructions is None and body.speed is None: raise HTTPException(422, "instructions or speed is required")
        try:
            if body.speed is None: await runtime.calls.steer(instructions)
            else: await runtime.calls.steer(instructions, body.speed)
            return {"ok": True}
        except RuntimeError as exc: raise HTTPException(409, str(exc)) from exc
        except ValueError as exc: raise HTTPException(422, str(exc)) from exc

    @app.get("/api/transcript")
    async def transcript(): return {"messages": runtime.calls.transcript()}

    @app.post("/api/messages")
    async def save_message(body: MessageInput):
        return runtime_message_store(runtime).save(body)

    @app.get("/api/messages")
    async def list_messages():
        return {"messages": runtime_message_store(runtime).list()}

    @app.post("/api/restart")
    async def restart(): await runtime.restart(runtime.config); return {"ok": True}

    return app
