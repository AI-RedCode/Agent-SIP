import hashlib
import hmac

import httpx
import pytest

from app.calls import CallSession
from app.config import WebhookConfig
from app.webhook import WebhookNotifier


@pytest.mark.asyncio
async def test_webhook_payload_auth_and_outbound_partial_gating():
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(204)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = WebhookConfig(
        enabled=True,
        webhook_url="https://hooks.test/events",
        auth_token="token",
        notify_partials_outgoing=False,
    )
    notifier = WebhookNotifier(config, "Ada", "http://127.0.0.1:8765", client)
    call = CallSession("call-1", "outbound", "201", local_number="200")
    call.add_transcript("caller", "hello")
    await notifier.notify("call.started", call)
    await notifier.notify("transcript.partial", call, partial=True)
    assert len(requests) == 1 and requests[0].headers["authorization"] == "Bearer token"
    expected = hmac.new(b"token", requests[0].content, hashlib.sha256).hexdigest()
    assert requests[0].headers["x-hub-signature-256"] == f"sha256={expected}"
    payload = __import__("json").loads(requests[0].content)
    assert set(payload) == {"type", "event", "call_id", "caller_number", "called_number", "agent_name", "transcript", "timestamp", "mcp_url"}
    assert payload["type"] == "call.started"
    assert payload["caller_number"] == "200" and payload["called_number"] == "201"
    config.notify_partials_outgoing = True
    await notifier.notify("transcript.partial", call, partial=True)
    assert len(requests) == 2
    assert requests[1].headers["x-hub-signature-256"] != requests[0].headers["x-hub-signature-256"]
    expected = hmac.new(b"token", requests[1].content, hashlib.sha256).hexdigest()
    assert requests[1].headers["x-hub-signature-256"] == f"sha256={expected}"
    await client.aclose()


@pytest.mark.asyncio
async def test_inbound_partials_are_disabled_by_default_and_can_be_enabled():
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(204)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = WebhookConfig(webhook_url="https://hooks.test/events")
    notifier = WebhookNotifier(config, "Agent", "", client)
    call = CallSession("id", "inbound", "201")

    await notifier.notify("transcript.partial", call, partial=True)
    assert requests == []
    config.notify_partials_incoming = True
    await notifier.notify("transcript.partial", call, partial=True)
    assert len(requests) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_full_events_ignore_partial_direction_settings():
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(204)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = WebhookConfig(
        webhook_url="https://hooks.test/events",
        notify_partials_incoming=False,
        notify_partials_outgoing=False,
    )
    notifier = WebhookNotifier(config, "Agent", "", client)
    inbound = CallSession("in", "inbound", "201")
    outbound = CallSession("out", "outbound", "201")

    await notifier.notify("call.started", inbound)
    await notifier.notify("call.ended", outbound)
    assert len(requests) == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_webhook_failure_is_swallowed(caplog):
    async def handler(request): raise httpx.ConnectError("offline")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(WebhookConfig(True, "https://hooks.test/events"), "Agent", "", client)
    await notifier.notify("call.ended", CallSession("id", "inbound", "201"))
    assert "Webhook delivery failed" in caplog.text
    await client.aclose()


@pytest.mark.asyncio
async def test_empty_webhook_url_is_a_no_op():
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(204)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(WebhookConfig(enabled=False), "Agent", "", client)
    await notifier.notify("call.started", CallSession("id", "inbound", "201"))
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_notify_posts_identical_body_to_both_webhook_urls():
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(204)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = WebhookConfig(
        webhook_url="https://primary.test/events",
        webhook_url2="https://secondary.test/events",
        auth_token="shared-token",
    )
    notifier = WebhookNotifier(config, "Agent", "", client)

    await notifier.notify("call.started", CallSession("id", "inbound", "201"))

    assert [str(request.url) for request in requests] == [
        "https://primary.test/events", "https://secondary.test/events",
    ]
    assert requests[0].content == requests[1].content
    assert requests[0].headers["x-hub-signature-256"] == requests[1].headers["x-hub-signature-256"]
    await client.aclose()


@pytest.mark.asyncio
async def test_notify_only_posts_to_primary_when_second_url_is_empty():
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(204)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(WebhookConfig(webhook_url="https://primary.test/events"), "Agent", "", client)

    await notifier.notify("call.started", CallSession("id", "inbound", "201"))

    assert [str(request.url) for request in requests] == ["https://primary.test/events"]
    await client.aclose()


@pytest.mark.asyncio
async def test_second_webhook_failure_does_not_affect_primary(caplog):
    requests = []
    async def handler(request):
        requests.append(request)
        if request.url.host == "secondary.test":
            raise httpx.ConnectError("secondary offline")
        return httpx.Response(204)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = WebhookConfig(
        webhook_url="https://primary.test/events",
        webhook_url2="https://secondary.test/events",
    )
    notifier = WebhookNotifier(config, "Agent", "", client)

    await notifier.notify("call.started", CallSession("id", "inbound", "201"))

    assert [request.url.host for request in requests] == ["primary.test", "secondary.test"]
    assert "Webhook delivery failed" in caplog.text
    assert "https://secondary.test/events" in caplog.text
    await client.aclose()


@pytest.mark.asyncio
async def test_ended_payload_includes_outcome_rings_and_duration():
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(204)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(WebhookConfig(True, "https://hooks.test/events"), "Agent", "", client)
    call = CallSession("id", "outbound", "201", rings=3, outcome="not_answered")
    call.ended_at = call.started_at
    await notifier.notify("call.ended", call)
    payload = __import__("json").loads(requests[0].content)
    assert payload["outcome"] == "not_answered" and payload["rings"] == 3
    assert payload["duration"] == 0
    await client.aclose()
