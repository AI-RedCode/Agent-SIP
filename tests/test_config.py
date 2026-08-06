import json

import pytest

from app.config import (
    AppConfig, ConfigStore, DEFAULT_AGENT_INSTRUCTIONS,
    DEFAULT_OUTBOUND_AGENT_INSTRUCTIONS,
)


REALTIME_VOICES = {"alloy", "ash", "ballad", "cedar", "coral", "echo", "marin", "sage", "shimmer", "verse"}


def test_config_defaults():
    cfg = AppConfig()
    assert cfg.agent.default_prompt == DEFAULT_AGENT_INSTRUCTIONS
    assert cfg.agent.inbound_prompt == DEFAULT_AGENT_INSTRUCTIONS
    assert cfg.agent.inbound_brief == ""
    assert cfg.agent.outbound_prompt == DEFAULT_OUTBOUND_AGENT_INSTRUCTIONS
    assert cfg.agent.default_language == "fr"
    assert "CALL BRIEF" in cfg.agent.inbound_prompt
    assert "## Saving the Message" in cfg.agent.inbound_prompt
    assert "save_message" in cfg.agent.inbound_prompt
    assert "hangup_call" in cfg.agent.inbound_prompt
    assert "Do not speak before calling save_message" in DEFAULT_AGENT_INSTRUCTIONS
    assert "Say nothing before or after this sentence" in DEFAULT_AGENT_INSTRUCTIONS
    assert "If the caller says only « Bonjour », reply:\n« Je vous écoute. »" in DEFAULT_AGENT_INSTRUCTIONS
    assert "At the start of the call, say exactly" in DEFAULT_AGENT_INSTRUCTIONS
    assert "Never repeat yourself" in DEFAULT_AGENT_INSTRUCTIONS
    assert "Never rephrase a sentence" in DEFAULT_AGENT_INSTRUCTIONS
    assert "ALWAYS use the exact recipient" in DEFAULT_AGENT_INSTRUCTIONS
    assert "## Role" in DEFAULT_AGENT_INSTRUCTIONS
    assert "## Language" in DEFAULT_AGENT_INSTRUCTIONS
    assert "```\n« C'est noté. Au revoir. »\n```" in DEFAULT_AGENT_INSTRUCTIONS
    assert "CALL BRIEF" in cfg.agent.outbound_prompt
    assert "Do not switch languages based on" in cfg.agent.outbound_prompt
    assert "Voulez-vous continuer en français" in cfg.agent.outbound_prompt
    assert "Parlez TOUJOURS en français" not in cfg.agent.outbound_prompt
    assert "Ne changez jamais de langue" not in cfg.agent.outbound_prompt
    assert "ONLY when calling on behalf of an individual" in cfg.agent.outbound_prompt
    assert "Je vous appelle de la part de" in cfg.agent.outbound_prompt
    assert "## Call Type" in cfg.agent.outbound_prompt
    assert "simple data collection never triggers" in cfg.agent.outbound_prompt
    assert "Never say bracketed placeholders" in cfg.agent.outbound_prompt
    assert "Never hang up mid-task" in cfg.agent.outbound_prompt
    assert "Pardon, pourriez-vous préciser ?" in cfg.agent.outbound_prompt
    assert "Merci beaucoup ! Au revoir !" in cfg.agent.outbound_prompt
    assert "The FINAL phrase" in cfg.agent.outbound_prompt
    assert "## Hard Rules" in cfg.agent.outbound_prompt
    assert "## Call Flow" in cfg.agent.outbound_prompt
    assert "## Role" in cfg.agent.outbound_prompt
    assert "## Language" in cfg.agent.outbound_prompt
    assert "```\n« Merci beaucoup ! Au revoir ! »\n```" in cfg.agent.outbound_prompt
    assert "always ask for the name" in cfg.agent.outbound_prompt
    assert cfg.agent.max_rings == 6
    assert cfg.agent.max_ring_seconds == 30
    assert cfg.agent.max_agent_turns == 3
    assert cfg.agent.end_grace_seconds == 4
    assert cfg.voice.voice in REALTIME_VOICES
    assert cfg.voice.voice == "marin"
    assert cfg.voice.speed == 1.0
    assert cfg.mcp.enabled is True
    assert (cfg.mcp.host, cfg.mcp.port, cfg.mcp.auth_token) == ("127.0.0.1", 8765, "")
    assert cfg.webhook.enabled is True
    assert cfg.webhook.webhook_url == ""
    assert cfg.webhook.webhook_url2 == ""
    assert cfg.webhook.auth_token == ""
    assert cfg.webhook.notify_partials_incoming is False
    assert cfg.webhook.notify_partials_outgoing is True
    assert (cfg.ambient.enabled, cfg.ambient.file, cfg.ambient.volume) == (False, "office.wav", 0.12)
    assert (cfg.web.enabled, cfg.web.username, cfg.web.password) == (True, "admin", "admin")


def test_config_roundtrip_and_mask(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    cfg = AppConfig(); cfg.sip.server_host = "pbx.test"; cfg.sip.password = "secret"; cfg.voice.api_key = "sk-test"; cfg.agent.inbound_brief = "Pour Monsieur X"; cfg.agent.default_language = "hy"; cfg.webhook.webhook_url2 = "https://automation.test/events"
    ConfigStore(path).save(cfg)
    loaded = ConfigStore(path).load()
    assert loaded.agent.inbound_prompt == DEFAULT_AGENT_INSTRUCTIONS
    assert loaded.agent.inbound_brief == "Pour Monsieur X"
    assert loaded.agent.outbound_prompt == DEFAULT_OUTBOUND_AGENT_INSTRUCTIONS
    assert loaded.agent.default_language == "hy"
    assert loaded.sip.server_host == "pbx.test"
    assert loaded.webhook.webhook_url2 == "https://automation.test/events"
    assert loaded.to_dict(True)["voice"]["api_key"] == "********"
    assert loaded.mcp.host == "127.0.0.1" and loaded.mcp.port == 8765
    assert loaded.ambient.file == "office.wav" and loaded.ambient.volume == 0.12
    assert json.loads(path.read_text())["sip"]["password"] == "secret"
    assert json.loads(path.read_text())["web"] == {"enabled": True, "username": "admin", "password": "admin"}
    monkeypatch.setenv("SIP_SERVER_PORT", "5070")
    monkeypatch.setenv("WEB_USERNAME", "operator")
    monkeypatch.setenv("WEB_PASSWORD", "web-secret")
    assert ConfigStore(path).load().sip.server_port == cfg.sip.server_port
    assert (ConfigStore(path).load().web.username, ConfigStore(path).load().web.password) == ("admin", "admin")


def test_webhook_url2_environment_default(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL2", "https://automation.test/events")
    cfg = ConfigStore(tmp_path / "missing.json").load()
    assert cfg.webhook.webhook_url2 == "https://automation.test/events"


def test_stale_inbound_prompt_is_self_healed_without_losing_brief(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"agent": {
        "inbound_prompt": "Old truncated prompt",
        "inbound_brief": "Messages pour Madame Dupont",
    }}))

    loaded = ConfigStore(path).load()

    assert loaded.agent.inbound_prompt == DEFAULT_AGENT_INSTRUCTIONS
    assert loaded.agent.inbound_brief == "Messages pour Madame Dupont"
    persisted = json.loads(path.read_text())
    assert persisted["agent"]["inbound_prompt"] == DEFAULT_AGENT_INSTRUCTIONS
    assert persisted["agent"]["inbound_brief"] == "Messages pour Madame Dupont"


def test_stale_outbound_language_rule_is_self_healed(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"agent": {
        "outbound_prompt": "Parlez TOUJOURS en français, même si votre interlocuteur parle une autre langue.",
    }}))

    loaded = ConfigStore(path).load()

    assert loaded.agent.outbound_prompt == DEFAULT_OUTBOUND_AGENT_INSTRUCTIONS
    assert json.loads(path.read_text())["agent"]["outbound_prompt"] == DEFAULT_OUTBOUND_AGENT_INSTRUCTIONS


def test_ambient_config_roundtrip_and_environment(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    cfg = AppConfig()
    cfg.ambient.enabled, cfg.ambient.file, cfg.ambient.volume = True, "office.wav", 0.2
    ConfigStore(path).save(cfg)
    loaded = ConfigStore(path).load()
    assert (loaded.ambient.enabled, loaded.ambient.file, loaded.ambient.volume) == (True, "office.wav", 0.2)
    monkeypatch.setenv("AMBIENT_ENABLED", "false")
    monkeypatch.setenv("AMBIENT_VOLUME", "0.3")
    loaded = ConfigStore(path).load()
    assert loaded.ambient.enabled is True and loaded.ambient.volume == 0.2


def test_environment_is_default_for_file_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"voice": {"api_key": "file-key"}}))
    monkeypatch.setenv("VOICE_API_KEY", "env-key")
    assert ConfigStore(path).load().voice.api_key == "file-key"

    path.write_text(json.dumps({"voice": {}}))
    assert ConfigStore(path).load().voice.api_key == "env-key"


def test_agent_default_language_environment_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DEFAULT_LANGUAGE", "en")
    assert ConfigStore(tmp_path / "missing.json").load().agent.default_language == "en"


def test_legacy_disabled_services_are_normalized():
    cfg = AppConfig.from_dict({"mcp": {"enabled": False}, "webhook": {"enabled": False}})
    assert cfg.mcp.enabled is True
    assert cfg.webhook.enabled is True


@pytest.mark.parametrize("legacy_value", [True, False])
def test_legacy_webhook_notify_partials_maps_to_both_directions(legacy_value):
    cfg = AppConfig.from_dict({"webhook": {"notify_partials": legacy_value}})
    assert cfg.webhook.notify_partials_incoming is legacy_value
    assert cfg.webhook.notify_partials_outgoing is legacy_value


def test_directional_webhook_partial_settings_override_legacy():
    cfg = AppConfig.from_dict({"webhook": {
        "notify_partials": False,
        "notify_partials_incoming": True,
    }})
    assert cfg.webhook.notify_partials_incoming is True
    assert cfg.webhook.notify_partials_outgoing is False


@pytest.mark.parametrize("legacy_value", ["true", "false"])
def test_legacy_webhook_notify_partials_environment_sets_both(tmp_path, monkeypatch, legacy_value):
    monkeypatch.setenv("WEBHOOK_NOTIFY_PARTIALS", legacy_value)
    cfg = ConfigStore(tmp_path / "missing.json").load()
    expected = legacy_value == "true"
    assert cfg.webhook.notify_partials_incoming is expected
    assert cfg.webhook.notify_partials_outgoing is expected


def test_legacy_webhook_file_setting_beats_legacy_environment(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"webhook": {"notify_partials": False}}))
    monkeypatch.setenv("WEBHOOK_NOTIFY_PARTIALS", "true")
    cfg = ConfigStore(path).load()
    assert cfg.webhook.notify_partials_incoming is False
    assert cfg.webhook.notify_partials_outgoing is False


def test_legacy_default_prompt_is_direction_fallback():
    cfg = AppConfig.from_dict({"agent": {"default_prompt": "Legacy prompt"}})
    assert cfg.agent.inbound_prompt == "Legacy prompt"
    assert cfg.agent.outbound_prompt == "Legacy prompt"


def test_config_validation():
    cfg = AppConfig(); cfg.sip.codec = "opus"
    with pytest.raises(ValueError): cfg.validate()

    cfg = AppConfig(); cfg.agent.default_language = " "
    with pytest.raises(ValueError, match="default_language"): cfg.validate()

    for speed in (0.24, 4.01):
        cfg = AppConfig(); cfg.voice.speed = speed
        with pytest.raises(ValueError, match="voice speed"):
            cfg.validate()

    for max_rings in (0, 21):
        cfg = AppConfig(); cfg.agent.max_rings = max_rings
        with pytest.raises(ValueError, match="max_rings"):
            cfg.validate()

    for max_ring_seconds in (4, 301):
        cfg = AppConfig(); cfg.agent.max_ring_seconds = max_ring_seconds
        with pytest.raises(ValueError, match="max_ring_seconds"):
            cfg.validate()


def test_ring_timeout_derives_from_max_rings_unless_explicit():
    assert AppConfig.from_dict({"agent": {"max_rings": 10}}).agent.max_ring_seconds == 50
    assert AppConfig.from_dict({"agent": {"max_rings": 10, "max_ring_seconds": 17}}).agent.max_ring_seconds == 17
