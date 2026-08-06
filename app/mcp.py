from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .messages import MessageInput, runtime_message_store

TOOLS = [
    {"name": "get_status", "description": "Get SIP registration and current call state", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "make_call", "description": "Start an outbound call", "inputSchema": {"type": "object", "properties": {"number": {"type": "string"}, "call_brief": {"type": ["string", "null"], "description": "CALL BRIEF — what the agent should say/ask on this call (the objective provided for this call)"}, "text": {"type": ["string", "null"], "description": "Deprecated alias for call_brief"}}, "required": ["number"]}},
    {"name": "hangup_call", "description": "Hang up the active call", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "say", "description": "Speak text into the active call", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "steer", "description": "Change the agent's instructions/tone or speaking speed mid-call", "inputSchema": {"type": "object", "properties": {"instructions": {"type": "string"}, "speed": {"type": "number", "minimum": 0.25, "maximum": 4.0}}}},
    {"name": "get_transcript", "description": "Get recent transcript messages", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "save_message", "description": "Save a caller-confirmed household message", "inputSchema": {"type": "object", "properties": {"recipient": {"type": "string", "enum": ["monsieur_mounier", "madame_astride"]}, "caller_name": {"type": "string"}, "message": {"type": "string"}, "callback_number": {"type": ["string", "null"]}, "language": {"type": "string", "enum": ["fr"]}, "confirmed_by_caller": {"type": "boolean"}}, "required": ["recipient", "caller_name", "message", "callback_number", "language", "confirmed_by_caller"]}},
]


class ToolCall(BaseModel):
    tool: str | None = None
    name: str | None = None
    arguments: dict = Field(default_factory=dict)


def status_result(runtime) -> dict:
    active = runtime.calls.active
    recent = active or (runtime.calls.history[-1] if runtime.calls.history else None)
    return {"sip_registered": bool(runtime.sip and runtime.sip.registered), "call_state": recent.state if recent else "idle", "active_call": active.public() if active else None, "last_call": recent.public() if recent else None}


def create_mcp_app(runtime) -> FastAPI:
    app = FastAPI(title="Agent-SIP MCP", version="0.1.0")

    async def authorize(authorization: str | None = Header(default=None)):
        token = runtime.config.mcp.auth_token
        if token and authorization != f"Bearer {token}":
            raise HTTPException(401, "invalid or missing bearer token", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/status", dependencies=[Depends(authorize)])
    async def status(): return status_result(runtime)

    @app.get("/tools", dependencies=[Depends(authorize)])
    async def tools(): return {"tools": TOOLS}

    @app.post("/call", dependencies=[Depends(authorize)])
    async def call(body: ToolCall):
        args = body.arguments
        tool_name = body.tool or body.name
        try:
            if tool_name == "get_status": result = status_result(runtime)
            elif tool_name == "make_call":
                number = str(args.get("number", "")).strip()
                if not number: raise ValueError("number is required")
                call_brief = args.get("call_brief") if "call_brief" in args else args.get("text")
                result = (await runtime.calls.make_call(number, call_brief)).public()
            elif tool_name == "hangup_call": await runtime.calls.hangup(); result = {"ok": True}
            elif tool_name == "say":
                value = str(args.get("text", "")).strip()
                if not value: raise ValueError("text is required")
                await runtime.calls.say(value); result = {"ok": True}
            elif tool_name == "steer":
                value = str(args.get("instructions", "")).strip() or None
                speed = args.get("speed")
                if value is None and speed is None: raise ValueError("instructions or speed is required")
                if speed is None: await runtime.calls.steer(value)
                else: await runtime.calls.steer(value, speed)
                result = {"ok": True}
            elif tool_name == "get_transcript": result = {"messages": runtime.calls.transcript()}
            elif tool_name == "save_message":
                result = runtime_message_store(runtime).save(MessageInput.model_validate(args))
            else: raise HTTPException(404, f"unknown tool: {tool_name}")
            return {"ok": True, "result": result}
        except ValueError as exc: raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc: raise HTTPException(409, str(exc)) from exc

    return app
