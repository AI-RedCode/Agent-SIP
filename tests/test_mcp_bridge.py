from __future__ import annotations

import os
import socket
import sys

import uvicorn
from fastapi import FastAPI
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def test_standard_mcp_stdio_round_trip():
    upstream = FastAPI()
    requests = []

    @upstream.post("/call")
    async def call(body: dict):
        requests.append(body)
        if body["name"] == "get_status":
            return {"ok": True, "result": {"sip_registered": True, "call_state": "idle"}}
        return {"ok": True, "result": {"number": body["arguments"]["number"], "state": "dialing"}}

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(upstream, log_level="error", lifespan="off"))
    server_task = __import__("asyncio").create_task(server.serve(sockets=[sock]))
    while not server.started:
        await __import__("asyncio").sleep(0)

    env = os.environ.copy()
    env["AGENT_SIP_API_URL"] = f"http://127.0.0.1:{port}"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_bridge"],
        env=env,
    )
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "Agent-SIP"

                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == [
                    "get_status", "make_call", "hangup_call", "say", "steer",
                    "get_transcript", "save_message",
                ]
                make_call_tool = next(tool for tool in listed.tools if tool.name == "make_call")
                assert make_call_tool.inputSchema["required"] == ["number"]

                status = await session.call_tool("get_status")
                assert status.isError is False
                assert status.structuredContent["sip_registered"] is True

                made = await session.call_tool("make_call", {
                    "number": "201", "call_brief": "Ask for Alex",
                })
                assert made.isError is False
                assert made.structuredContent["number"] == "201"
    finally:
        server.should_exit = True
        await server_task

    assert requests == [
        {"name": "get_status", "arguments": {}},
        {"name": "make_call", "arguments": {"number": "201", "call_brief": "Ask for Alex"}},
    ]
