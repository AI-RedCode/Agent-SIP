"""Standard MCP stdio bridge for Agent-SIP's local control API."""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INTERNAL_ERROR


UPSTREAM_URL = os.environ.get("AGENT_SIP_API_URL", "http://127.0.0.1:8765").rstrip("/")
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN")

mcp = FastMCP(
    "Agent-SIP",
    instructions="Control the running Agent-SIP voice application.",
    log_level="WARNING",
)


async def _call(name: str, arguments: dict[str, Any] | None = None) -> Any:
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"} if AUTH_TOKEN else {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{UPSTREAM_URL}/call",
                json={"name": name, "arguments": arguments or {}},
                headers=headers,
            )
        response.raise_for_status()
        payload = response.json()
    except httpx.ConnectError as exc:
        raise McpError(ErrorData(
            code=INTERNAL_ERROR,
            message=f"Agent-SIP is not running or unreachable at {UPSTREAM_URL}",
        )) from exc
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except ValueError:
            detail = exc.response.text
        raise McpError(ErrorData(
            code=INTERNAL_ERROR,
            message=f"Agent-SIP API returned {exc.response.status_code}: {detail}",
        )) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Agent-SIP API error: {exc}")) from exc

    if not payload.get("ok"):
        raise McpError(ErrorData(code=INTERNAL_ERROR, message="Agent-SIP tool call failed"))
    return payload.get("result")


@mcp.tool(description="Get SIP registration and current call state")
async def get_status() -> dict[str, Any]:
    return await _call("get_status")


@mcp.tool(description="Start an outbound call")
async def make_call(number: str, call_brief: str | None = None) -> dict[str, Any]:
    return await _call("make_call", {"number": number, "call_brief": call_brief})


@mcp.tool(description="Hang up the active call")
async def hangup_call() -> dict[str, Any]:
    return await _call("hangup_call")


@mcp.tool(description="Speak text into the active call")
async def say(text: str) -> dict[str, Any]:
    return await _call("say", {"text": text})


@mcp.tool(description="Change the agent's instructions/tone or speaking speed mid-call")
async def steer(instructions: str | None = None, speed: float | None = None) -> dict[str, Any]:
    arguments = {}
    if instructions is not None:
        arguments["instructions"] = instructions
    if speed is not None:
        arguments["speed"] = speed
    return await _call("steer", arguments)


@mcp.tool(description="Get recent transcript messages")
async def get_transcript() -> dict[str, Any]:
    return await _call("get_transcript")


@mcp.tool(description="Save a caller-confirmed household message")
async def save_message(
    recipient: str,
    caller_name: str,
    message: str,
    callback_number: str | None,
    language: str,
    confirmed_by_caller: bool,
) -> dict[str, Any]:
    return await _call("save_message", {
        "recipient": recipient,
        "caller_name": caller_name,
        "message": message,
        "callback_number": callback_number,
        "language": language,
        "confirmed_by_caller": confirmed_by_caller,
    })


def cli() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    cli()
