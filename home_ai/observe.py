"""Opt-in HA event subscription. No HA service calls or configuration writes."""
import asyncio
import json
import os
from urllib.parse import urlparse, urlunparse
from .pipeline import Pipeline


def project_event(message, mapping):
    event = message.get("event", {})
    data = event.get("data", {})
    entity = data.get("entity_id")
    if event.get("event_type") != "state_changed" or entity not in mapping:
        return None
    state = data.get("new_state") or {}
    attrs = state.get("attributes") or {}
    domain = entity.split(".")[0]
    kind = "robot" if domain == "vacuum" else "presence" if attrs.get("device_class") in ("motion", "occupancy", "presence") else "environment" if domain == "sensor" else "device"
    context = state.get("context") or {}
    alias = mapping[entity]
    return {"id": str(context.get("id") or "event") + ":" + alias + ":" + str(event.get("time_fired")),
            "ts": event["time_fired"], "entity_id": alias, "state": str(state.get("state", "unknown")),
            "kind": kind, "source": "manual" if context.get("user_id") else "automation" if context.get("parent_id") else "system",
            "area": alias.split(".")[0]}


async def observe(cfg, provider, limit, emit):
    import websockets
    token = os.environ.get("HOME_AI_HA_TOKEN", "")
    url = urlparse(cfg["ha_url"])
    if not token or not cfg["entity_mapping"]:
        raise ValueError("Set HOME_AI_HA_TOKEN and explicit entity_mapping")
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Invalid HA URL")
    ws_url = urlunparse(("wss" if url.scheme == "https" else "ws", url.netloc, url.path.rstrip("/")+"/api/websocket", "", "", ""))
    pipeline = Pipeline(cfg, provider)
    async with websockets.connect(ws_url, open_timeout=10, max_size=1048576, proxy=None) as ws:
        async def receive():
            return json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        if (await receive()).get("type") != "auth_required":
            raise ValueError("Unexpected HA greeting")
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        if (await receive()).get("type") != "auth_ok":
            raise ValueError("HA authentication failed")
        await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
        if not (await receive()).get("success"):
            raise ValueError("HA subscription failed")
        emit({"status": "observing", "mode": "shadow"})
        count = 0
        async for raw in ws:
            projected = project_event(json.loads(raw), cfg["entity_mapping"])
            if projected:
                try:
                    result = await asyncio.to_thread(pipeline.feed, projected)
                except ValueError:
                    continue  # malformed or late events do not stop observation
                count += 1
                if result:
                    emit(result)
                if limit and count >= limit:
                    break
    return pipeline.metrics()
