import json
from collections import deque, OrderedDict
from datetime import datetime, timezone
from .provider import FixtureProvider, validate_decision


def timestamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return dt.astimezone(timezone.utc)


def normalize(raw):
    required = ("id", "ts", "entity_id", "state", "kind", "source")
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) and 0 < len(raw[k]) <= 160 for k in required):
        raise ValueError("Invalid normalized event")
    if raw["kind"] not in ("presence", "robot", "device", "environment") or raw["source"] not in ("system", "manual", "automation"):
        raise ValueError("Unsupported event kind/source")
    timestamp(raw["ts"])
    event = {k: raw[k] for k in required}
    event["area"] = str(raw.get("area", "example_area"))[:80]
    return event


class Pipeline:
    def __init__(self, cfg, provider=None):
        self.cfg, self.provider = cfg, provider or FixtureProvider()
        self.events = deque(maxlen=cfg["max_events"])
        self.seen = OrderedDict()
        self.last_call = {}
        self.latest = None
        self.total = self.accepted = self.calls = self.failures = 0

    def feed(self, raw):
        self.total += 1
        event = normalize(raw)
        now = timestamp(event["ts"])
        if self.latest and now < self.latest:
            raise ValueError("Events must be ordered by timestamp")
        self.latest = now
        while self.events and (now - timestamp(self.events[0]["ts"])).total_seconds() > self.cfg["max_age_seconds"]:
            self.events.popleft()
        self.last_call = {k:v for k,v in self.last_call.items() if (now-v).total_seconds() < self.cfg["cooldown_seconds"]}
        if event["id"] in self.seen:
            return None
        self.seen[event["id"]] = True
        if len(self.seen) > self.cfg["max_events"] * 2:
            self.seen.popitem(last=False)
        previous = next((e for e in reversed(self.events) if e["entity_id"] == event["entity_id"]), None)
        if previous and (previous["state"], previous["source"]) == (event["state"], event["source"]):
            self.events.append(event)  # retain freshness, skip redundant inference
            return None
        self.accepted += 1
        self.events.append(event)
        reason = None
        recent = [e for e in self.events if (now-timestamp(e["ts"])).total_seconds() <= 180]
        if event["source"] == "manual" and previous and previous["source"] == "automation" and previous["state"] != event["state"] and (now-timestamp(previous["ts"])).total_seconds() <= 180:
            reason = "automation_correction"
        if event["kind"] == "presence":
            states = {}
            for e in recent:
                if e["area"] == event["area"]:
                    states[e["entity_id"]] = e
            if any(e["kind"] == "presence" and e["entity_id"] != event["entity_id"] and e["state"] != event["state"] for e in states.values()):
                reason = "sensor_conflict"
            elif event["state"] == "on" and any(e["kind"] == "robot" and e["state"] == "cleaning" for e in states.values()):
                reason = "possible_robot_false_positive"
        if not reason:
            return None
        key = (event["entity_id"], reason)
        if key in self.last_call:
            return None
        if len(self.last_call) >= self.cfg["max_events"]:
            return None  # bounded per-window inference budget
        self.last_call[key] = now
        payload = {"trigger": reason, "events": list(self.events)}
        while len(json.dumps(payload, ensure_ascii=True)) > self.cfg["max_context_chars"] and len(payload["events"]) > 1:
            payload["events"].pop(0)
        if len(json.dumps(payload, ensure_ascii=True)) > self.cfg["max_context_chars"]:
            return {"decision": "abstain", "reason_code": "context_budget_exceeded", "evidence": [],
                    "explanation": "Event exceeds configured input budget.", "uncertainty": "Not analyzed.", "executed": False}
        self.calls += 1
        try:
            result = validate_decision(self.provider.analyze(payload), {e["id"] for e in payload["events"]})
        except Exception:
            self.failures += 1
            result = {"decision": "abstain", "reason_code": "provider_unavailable_or_invalid",
                      "evidence": [event["id"]], "explanation": "Inference failed; observation continues.",
                      "uncertainty": "No valid model judgment available."}
        return {**result, "executed": False, "provider": self.cfg["provider"]}

    def metrics(self):
        return {"input_events": self.total, "accepted_events": self.accepted, "provider_calls": self.calls,
                "provider_failures": self.failures, "buffered_events": len(self.events), "executed_actions": 0}
