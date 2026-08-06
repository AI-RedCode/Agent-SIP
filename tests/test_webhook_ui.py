from pathlib import Path


def test_webhook_settings_have_directional_partial_checkboxes():
    html = Path("static/index.html").read_text()
    assert "notify_partials_incoming:'Notify partials (incoming)'" in html
    assert "notify_partials_outgoing:'Notify partials (outgoing)'" in html
    assert "notify_partials_incoming:false" in html
    assert "notify_partials_outgoing:true" in html


def test_webhook_settings_have_second_full_width_url_field():
    html = Path("static/index.html").read_text()
    assert "webhook_url2:'Second webhook URL (optional)'" in html
    assert "webhook_url2:''" in html
    assert "key==='webhook_url2'" in html


def test_agent_settings_have_default_language_field():
    html = Path("static/index.html").read_text()
    assert "default_language:'Default language'" in html
    assert "agent:{default_language:'fr'}" in html
    assert "fr / en / hy ..." in html
