from pathlib import Path

from fastapi.testclient import TestClient

from app.calls import CallManager
from app.config import AppConfig, ConfigStore
from app.sip import SIPUserAgent
from app.web import create_app


class SIP:
    registered=True
    async def invite(self, number): return await self.calls.start("out", "outbound", number)
    async def cancel(self, session): pass
    async def bye(self, session): pass


class Runtime:
    def __init__(self,tmp_path):
        self.store=ConfigStore(tmp_path/"config.json"); self.config=AppConfig(); self.store.save(self.config)
        self.calls=CallManager(); self.sip=SIP(); self.sip.calls=self.calls; self.calls.sip=self.sip
        self.logs=[]; self.restarts=0
    async def restart(self,cfg): self.config=cfg; self.restarts+=1


def authenticated_client(runtime):
    client = TestClient(create_app(runtime))
    assert client.post("/api/login", json={"username": "admin", "password": "admin"}).status_code == 200
    return client


def test_login_and_route_protection(tmp_path):
    client = TestClient(create_app(Runtime(tmp_path)))
    assert client.get("/api/config").status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert '<form class="card" id="login">' in client.get("/login").text
    assert client.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    response = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert response.status_code == 200
    assert "agent_sip_session=" in response.headers["set-cookie"]
    assert client.get("/api/config").status_code == 200
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/config").status_code == 401


def test_changing_web_credentials_changes_login(tmp_path):
    rt = Runtime(tmp_path)
    client = authenticated_client(rt)
    cfg = client.get("/api/config").json()
    cfg["web"].update(username="operator", password="new-secret")
    assert client.put("/api/config", json=cfg).status_code == 200
    assert client.post("/api/login", json={"username": "admin", "password": "admin"}).status_code == 401
    assert client.post("/api/login", json={"username": "operator", "password": "new-secret"}).status_code == 200
    persisted = ConfigStore(tmp_path / "config.json").load()
    assert (persisted.web.username, persisted.web.password) == ("operator", "new-secret")


def test_status_config_and_controls(tmp_path):
    rt=Runtime(tmp_path)
    rt.config.sip.password="sip-secret"
    rt.config.voice.api_key="voice-secret"
    rt.config.mcp.auth_token="mcp-secret"
    rt.config.webhook.auth_token="webhook-secret"
    client=authenticated_client(rt)
    status = client.get("/api/status").json()
    assert status["sip_registered"] is True
    assert status["voice"] == {"configured": True, "connected": False, "last_error": None, "last_check": None}
    cfg=client.get("/api/config").json()
    assert cfg["sip"]["password"] == "sip-secret"
    assert cfg["voice"]["api_key"] == "voice-secret"
    assert cfg["mcp"]["auth_token"] == "mcp-secret"
    assert cfg["webhook"]["auth_token"] == "webhook-secret"
    cfg["agent"]["name"]="Ada"
    assert client.put("/api/config",json=cfg).status_code==200 and rt.config.agent.name=="Ada"
    assert rt.config.sip.password == "sip-secret"
    assert rt.config.voice.api_key == "voice-secret"
    assert rt.config.mcp.auth_token == "mcp-secret"
    assert rt.config.webhook.auth_token == "webhook-secret"
    assert client.post("/api/call",json={"number":"201"}).status_code==200
    assert client.post("/api/say",json={"text":"hello"}).status_code==409
    assert client.post("/api/hangup").json()=={"ok":True}
    assert client.get("/api/transcript").json()=={"messages":[]}
    current = rt.config
    rt.store.load = lambda: (_ for _ in ()).throw(AssertionError("restart reloaded config"))
    assert client.post("/api/restart").status_code==200
    assert rt.config is current


def test_voice_status_is_not_configured_without_api_key(tmp_path):
    voice = authenticated_client(Runtime(tmp_path)).get("/api/status").json()["voice"]
    assert voice["configured"] is False
    assert voice["connected"] is False
    assert voice["last_error"] is None


def test_validation(tmp_path):
    client=authenticated_client(Runtime(tmp_path))
    assert client.post("/api/call",json={"number":""}).status_code==422
    assert client.post("/api/say",json={"text":""}).status_code==422
    assert client.post("/api/steer",json={"instructions":""}).status_code==422


def test_steer(tmp_path):
    rt = Runtime(tmp_path)
    session = __import__("asyncio").run(rt.calls.start("id", "inbound", "201"))
    session.state = "active"
    steered = []

    class Realtime:
        async def steer(self, text): steered.append(text)

    session.realtime = Realtime()
    response = authenticated_client(rt).post("/api/steer", json={"instructions": "Be warmer"})
    assert response.json() == {"ok": True}
    assert steered == ["Be warmer"]


def test_call_accepts_call_brief_and_prefers_it_to_legacy_text(tmp_path):
    rt = Runtime(tmp_path)
    briefs = []
    original = rt.calls.make_call

    async def capture(number, call_brief=None):
        briefs.append(call_brief)
        return await original(number, call_brief)

    rt.calls.make_call = capture
    response = authenticated_client(rt).post(
        "/api/call", json={"number": "201", "call_brief": "Primary", "text": "Legacy"}
    )
    assert response.status_code == 200
    assert briefs == ["Primary"]


def test_messages_round_trip(tmp_path):
    client = authenticated_client(Runtime(tmp_path))
    message = {
        "recipient": "monsieur_mounier",
        "caller_name": "Camille",
        "message": "Merci de rappeler demain.",
        "callback_number": None,
        "language": "fr",
        "confirmed_by_caller": True,
    }
    saved = client.post("/api/messages", json=message)
    assert saved.status_code == 200
    assert saved.json()["ok"] is True
    listed = client.get("/api/messages").json()["messages"]
    assert listed[0]["message_id"] == saved.json()["message_id"]
    assert {key: listed[0][key] for key in message} == message


def test_call_without_transport_returns_clean_error_and_emits_started(tmp_path, caplog):
    rt = Runtime(tmp_path)
    rt.sip = SIPUserAgent(rt.config.sip, rt.calls)
    rt.sip.rtp_port = rt.config.sip.rtp_port_start
    rt.calls.sip = rt.sip
    events = []
    rt.calls.event_handler = lambda event, session, partial: events.append(event)

    response = authenticated_client(rt).post("/api/call", json={"number": "201"})

    assert response.status_code == 503
    assert response.json() == {"ok": False, "error": "SIP transport not ready"}
    assert events[:1] == ["call.started"]
    assert rt.calls.active is None
    assert "Outbound call rejected: SIP transport not ready" in caplog.text


def test_ui_contains_direction_specific_prompt_textareas(tmp_path):
    html = authenticated_client(Runtime(tmp_path)).get("/").text
    assert "Inbound prompt (incoming calls)" in html
    assert "Inbound brief (incoming calls)" in html
    assert "Recipients or context for incoming calls" in html
    assert "Outbound prompt (outgoing calls)" in html
    assert "key==='inbound_prompt'||key==='inbound_brief'||key==='outbound_prompt'" in html
    assert 'for="call_brief">Call Brief</label>' in html
    assert "JSON.stringify({number,call_brief})" in html


def test_ui_contains_voice_provider_status_states(tmp_path):
    html = authenticated_client(Runtime(tmp_path)).get("/").text
    assert "Voice Provider" in html
    assert "Speaking speed" in html
    assert "0.25–4.0 (1.0 = normal)" in html
    assert "Not configured" in html
    assert "Connected" in html
    assert "Disconnected" in html
    assert "s.voice.configured&&s.voice.connected" in html


def test_ui_statusbar_has_five_equal_columns(tmp_path):
    html = authenticated_client(Runtime(tmp_path)).get("/").text
    statusbar = html[html.index('<section class="statusbar">'):html.index("</section>")]
    assert statusbar.count('<div class="tile">') == 5
    assert '<span class="label">Agent</span>' in statusbar
    assert ".statusbar{grid-template-columns:repeat(5,1fr)" in html


def test_ui_places_full_width_agent_block_after_webhook(tmp_path):
    html = authenticated_client(Runtime(tmp_path)).get("/").text
    agent = '<div class="block full"><h3>Agent</h3><div class="fields" id="agentFields"></div></div>'
    webhook = '<div class="block"><h3>Webhook</h3><div class="fields" id="webhookFields"></div></div>'
    assert html.index(agent) > html.index(webhook)
    assert ".block.full{grid-column:1/-1}" in html


def test_ui_uses_three_tabs_with_agent_settings_separated(tmp_path):
    html = authenticated_client(Runtime(tmp_path)).get("/").text
    assert html.count('role="tab"') == 3
    assert 'data-tab="call-panel">Call' in html
    assert 'data-tab="settings-panel">Settings' in html
    assert 'data-tab="agent-panel">Agent' in html
    assert html.count('role="tabpanel"') == 3
    settings = html[html.index('id="settings-panel"'):html.index('id="agent-panel"')]
    agent = html[html.index('id="agent-panel"'):html.index('<script>')]
    assert 'id="agentFields"' not in settings
    assert 'id="agentFields"' in agent
    assert "switchTab('call-panel')" in html


def test_ui_contains_web_settings(tmp_path):
    html = authenticated_client(Runtime(tmp_path)).get("/").text
    assert '<div class="block"><h3>UI Password</h3><div class="fields" id="webFields"></div></div>' in html
    assert "web:{username:'Username',password:'Password'}" in html
    assert "web:{username:'admin',password:'admin'}" in html


def test_ui_renders_real_secret_in_password_input(tmp_path):
    html = authenticated_client(Runtime(tmp_path)).get("/").text
    assert "?'password':" in html
    assert '` value="${esc(val)}"`' in html
    assert "input.type=show?'text':'password'" in html


def test_ui_omits_service_enabled_toggles(tmp_path):
    html = authenticated_client(Runtime(tmp_path)).get("/").text
    assert "mcp:{enabled" not in html
    assert "webhook:{enabled" not in html


def test_ui_password_fields_have_visibility_toggles(tmp_path):
    html = authenticated_client(Runtime(tmp_path)).get("/").text
    assert "if(type==='password')" in html
    assert 'data-action="toggle-visibility"' in html
    assert "input.type=show?'text':'password'" in html
    assert 'data-s="${section}" data-k="${key}"' in html


def test_ambient_files_and_ui_controls(tmp_path):
    client = authenticated_client(Runtime(tmp_path))
    assert client.get("/api/ambient/files").json()["files"] == [
        "cafe.wav",
        "office.wav",
        "street.wav",
        "typing.wav",
    ]
    html = client.get("/").text
    assert "Background noise" in html
    assert 'id="ambient-enabled"' in html
    assert 'id="ambient-file"' in html
    assert 'id="ambient-volume"' in html
    assert "max_rings:'Max rings'" in html
    assert "max_ring_seconds:'Ring timeout (seconds)'" in html
    assert "max_ring_seconds\"]').value=Number(event.target.value)*5" in html
