from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)


class WebhookNotifier:
    """Best-effort delivery of call events to an external controller."""

    def __init__(self, config, agent_name: str, mcp_url: str, client=None):
        self.config = config
        self.agent_name = agent_name
        self.mcp_url = mcp_url
        self.client = client
        self.tasks: set[asyncio.Task] = set()

    def dispatch(self, event: str, call, partial: bool = False) -> None:
        """Queue delivery so a slow webhook cannot hold up call audio or signaling."""
        task = asyncio.create_task(self.notify(event, call, partial))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def notify(self, event: str, call, partial: bool = False) -> None:
        urls = [url for url in (self.config.webhook_url, self.config.webhook_url2) if url.strip()]
        if not urls:
            return
        if partial:
            if call.direction == "inbound" and not self.config.notify_partials_incoming:
                return
            if call.direction == "outbound" and not self.config.notify_partials_outgoing:
                return
        payload = {
            "type": event,
            "event": event,
            "call_id": call.call_id,
            "caller_number": call.number if call.direction == "inbound" else call.local_number,
            "called_number": call.local_number if call.direction == "inbound" else call.number,
            "agent_name": self.agent_name,
            "transcript": [vars(message) for message in call.transcript[-50:]],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mcp_url": self.mcp_url,
        }
        if event == "call.ended":
            payload.update(outcome=call.outcome, rings=call.rings, duration=call.duration)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.auth_token:
            secret = self.config.auth_token.encode("utf-8")
            headers.update({
                "Authorization": f"Bearer {self.config.auth_token}",
                "X-Hub-Signature-256": "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest(),
                "X-Hub-Signature": "sha1=" + hmac.new(secret, body, hashlib.sha1).hexdigest(),
            })
        if self.client:
            await self._post_all(self.client, urls, event, body, headers)
        else:
            async with httpx.AsyncClient(timeout=5) as client:
                await self._post_all(client, urls, event, body, headers)

    @staticmethod
    async def _post_all(client, urls, event, body, headers) -> None:
        for url in urls:
            try:
                response = await client.post(url, content=body, headers=headers)
                response.raise_for_status()
            except Exception as exc:
                log.warning("Webhook delivery failed for %s to %s: %s", event, url, exc)
