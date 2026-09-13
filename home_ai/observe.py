"""Explicit HA subscription with bounded ingestion; no device service calls."""
import asyncio
import hashlib
import json
import os
import time
from urllib.parse import urlunparse
from .config import ConfigError, validate_config, validate_endpoint
from .pipeline import Pipeline, normalize


def _object(value):
    if not isinstance(value, dict):
        raise ValueError("Malformed HA message")
    return value


def project_event(message, mapping):
    event = _object(_object(message).get("event", {}))
    if event.get("event_type") != "state_changed":
        return None
    data = _object(event.get("data", {}))
    entity = data.get("entity_id")
    if not isinstance(entity, str):
        raise ValueError("Malformed entity")
    if entity not in mapping:
        return None
    if data.get("new_state") is None:
        return None  # entity removed
    state = _object(data["new_state"])
    attrs = _object(state.get("attributes") or {})
    context = _object(state.get("context") or {})
    if not isinstance(event.get("time_fired"), str) or not isinstance(state.get("state"), str):
        raise ValueError("Malformed state or timestamp")
    domain = entity.split(".")[0]
    kind = "robot" if domain == "vacuum" else "presence" if attrs.get("device_class") in ("motion", "occupancy", "presence") else "environment" if domain == "sensor" else "device"
    alias = mapping[entity]
    identity = json.dumps([context.get("id"), alias, event["time_fired"], state["state"]], ensure_ascii=True)
    return normalize({"id": hashlib.sha256(identity.encode()).hexdigest(),
            "ts": event["time_fired"], "entity_id": alias, "state": state["state"],
            "kind": kind, "source": "manual" if context.get("user_id") else "automation" if context.get("parent_id") else "system",
            "area": alias.split(".")[0]})


async def observe(cfg, provider, limit, emit):
    cfg = validate_config(cfg)
    url = validate_endpoint(cfg["ha_url"], cfg["allow_remote_ha"], "HA")
    token = os.environ.get("HOME_AI_HA_TOKEN", "")
    if not token or not cfg["entity_mapping"] or type(limit) is not int or limit < 0:
        raise ConfigError("Set HOME_AI_HA_TOKEN, entity_mapping and a nonnegative observation limit")
    import websockets
    ws_url = urlunparse(("wss" if url.scheme == "https" else "ws", url.netloc, url.path.rstrip("/")+"/api/websocket", "", "", ""))
    pipeline = Pipeline(cfg, provider)
    queue = asyncio.Queue(maxsize=cfg["queue_capacity"])
    counters = {"received_messages": 0, "mapped_messages": 0, "ignored_messages": 0,
                "skipped_malformed": 0, "skipped_late": 0, "queue_dropped": 0,
                "queue_expired": 0, "queue_peak": 0, "processed_messages": 0}
    connection = websockets.connect(ws_url, open_timeout=10, max_size=1048576, max_queue=16, proxy=None)
    # websockets 15.0.1 follows HTTP redirects by default; preserve configured endpoint.
    connection.process_redirect = lambda exc: exc
    try:
        async with connection as ws:
            async def receive():
                return _object(json.loads(await asyncio.wait_for(ws.recv(), timeout=30)))
            if (await receive()).get("type") != "auth_required":
                raise ValueError("Unexpected HA greeting")
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            if (await receive()).get("type") != "auth_ok":
                raise ValueError("HA authentication failed")
            await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
            subscription = await receive()
            if subscription.get("success") is not True:
                raise ValueError("HA subscription failed")
            emit({"status": "observing", "mode": "shadow"})

            async def ingest():
                async for raw in ws:
                    counters["received_messages"] += 1
                    try:
                        projected = project_event(json.loads(raw), cfg["entity_mapping"])
                    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
                        counters["skipped_malformed"] += 1
                        continue
                    if projected is None:
                        counters["ignored_messages"] += 1
                        continue
                    counters["mapped_messages"] += 1
                    try:
                        queue.put_nowait((time.monotonic(), projected))
                        counters["queue_peak"] = max(counters["queue_peak"], queue.qsize())
                    except asyncio.QueueFull:
                        counters["queue_dropped"] += 1  # drop newest; never grow without bound
                    if limit and counters["mapped_messages"] >= limit:
                        break
                await queue.put(None)

            async def consume():
                while True:
                    item = await queue.get()
                    if item is None:
                        return
                    enqueued, event = item
                    if time.monotonic() - enqueued > cfg["max_queue_age_seconds"]:
                        counters["queue_expired"] += 1
                        continue
                    try:
                        result = await asyncio.to_thread(pipeline.feed, event)
                    except ValueError:
                        counters["skipped_late"] += 1
                        continue
                    counters["processed_messages"] += 1
                    if result:
                        emit(result)

            async with asyncio.TaskGroup() as group:
                group.create_task(ingest())
                group.create_task(consume())
    except BaseException:
        emit({"status": "observation_stopped", **pipeline.metrics(), **counters})
        raise
    return {**pipeline.metrics(), **counters}
