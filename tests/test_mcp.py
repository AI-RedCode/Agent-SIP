from fastapi.testclient import TestClient

from app.calls import CallManager
from app.config import AppConfig
from app.mcp import create_mcp_app
from app.messages import MessageStore


class Realtime:
    def __init__(self): self.spoken = []; self.steered = []
    async def say(self, text): self.spoken.append(text)
    async def steer(self, text): self.steered.append(text)
    async def stop(self): pass


class SIP:
    registered = True
    def __init__(self, calls): self.calls = calls
    async def invite(self, number): return await self.calls.start("out-1", "outbound", number)
    async def bye(self, session): pass


class Runtime:
    def __init__(self):
        self.config = AppConfig(); self.calls = CallManager(); self.sip = SIP(self.calls); self.calls.sip = self.sip


def invoke(client, name, arguments=None, headers=None):
    return client.post("/call", json={"name": name, "arguments": arguments or {}}, headers=headers or {})


def test_all_mcp_tools_share_call_manager():
    runtime = Runtime(); client = TestClient(create_mcp_app(runtime))
    tools = client.get("/tools").json()["tools"]
    assert [tool["name"] for tool in tools] == [
        "get_status", "make_call", "hangup_call", "say", "steer", "get_transcript", "save_message"
    ]
    make_call_schema = next(tool for tool in tools if tool["name"] == "make_call")["inputSchema"]
    assert "call_brief" in make_call_schema["properties"]
    assert make_call_schema["properties"]["call_brief"]["description"].startswith("CALL BRIEF")
    assert invoke(client, "get_status").json()["result"]["sip_registered"] is True
    assert invoke(client, "make_call", {"number": "201"}).json()["result"]["number"] == "201"
    runtime.calls.active.state = "active"; runtime.calls.active.realtime = Realtime()
    runtime.calls.active.add_transcript("caller", "hello")
    assert invoke(client, "say", {"text": "hi"}).json()["result"] == {"ok": True}
    assert runtime.calls.active.realtime.spoken == ["hi"]
    assert invoke(client, "steer", {"instructions": "Be warmer"}).json()["result"] == {"ok": True}
    assert runtime.calls.active.realtime.steered == ["Be warmer"]
    assert invoke(client, "get_transcript").json()["result"]["messages"][0]["text"] == "hello"
    assert invoke(client, "hangup_call").json()["result"] == {"ok": True}
    assert runtime.calls.active is None


def test_make_call_accepts_call_brief_and_legacy_text():
    runtime = Runtime(); client = TestClient(create_mcp_app(runtime))
    briefs = []
    original = runtime.calls.make_call

    async def capture(number, call_brief=None):
        briefs.append(call_brief)
        return await original(number, call_brief)

    runtime.calls.make_call = capture
    assert invoke(client, "make_call", {"number": "201", "call_brief": "Primary"}).status_code == 200
    runtime.calls.active = None
    assert invoke(client, "make_call", {"number": "202", "text": "Legacy"}).status_code == 200
    assert briefs == ["Primary", "Legacy"]


def test_save_message_tool(tmp_path):
    runtime = Runtime()
    runtime.message_store = MessageStore(tmp_path / "messages.json")
    client = TestClient(create_mcp_app(runtime))
    result = invoke(client, "save_message", {
        "recipient": "madame_astride",
        "caller_name": "Alex",
        "message": "Le colis arrivera demain.",
        "callback_number": "01 23 45 67 89",
        "language": "fr",
        "confirmed_by_caller": True,
    })
    assert result.status_code == 200
    assert result.json()["result"]["ok"] is True
    assert runtime.message_store.list()[0]["caller_name"] == "Alex"


def test_mcp_bearer_auth():
    runtime = Runtime(); runtime.config.mcp.auth_token = "secret"
    client = TestClient(create_mcp_app(runtime))
    assert client.get("/status").status_code == 401
    headers = {"Authorization": "Bearer secret"}
    assert client.get("/status", headers=headers).status_code == 200
    assert client.get("/tools", headers=headers).status_code == 200
    assert invoke(client, "get_status", headers=headers).status_code == 200
