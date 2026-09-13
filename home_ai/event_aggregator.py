from collections import OrderedDict
from datetime import datetime, timezone


class EventAggregator:
    def __init__(self, database, correction_window_seconds=180, max_entities=1000):
        self.db = database
        self.environment = OrderedDict()
        if type(max_entities) is not int or max_entities < 1:
            raise ValueError("max_entities must be positive")
        self.max_entities = max_entities
        self.recent_automated = {}
        self.correction_window = correction_window_seconds

    def process(self, event):
        if event["category"] == "environment":
            try:
                value = float(event["new_state"])
                import math
                if not math.isfinite(value):
                    return None
                entity = event["entity_id"]
                if entity not in self.environment and len(self.environment) >= self.max_entities:
                    self._flush_one()
                point = self.environment.get(entity)
                if point is None:
                    self.environment[entity] = dict(count=1, total=value, min=value, max=value, first=value, last=value, event=event.copy())
                else:
                    point.update(count=point["count"]+1, total=point["total"]+value, min=min(point["min"],value), max=max(point["max"],value), last=value, event=event.copy())
            except (TypeError, ValueError):
                pass
            return None
        now = datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))
        entity = event.get("entity_id")
        self.recent_automated = {k:v for k,v in self.recent_automated.items() if 0 <= (now-v[0]).total_seconds() <= self.correction_window}
        if len(self.recent_automated) >= self.max_entities:
            self.recent_automated.pop(next(iter(self.recent_automated)))
        if event["source"] == "automation" and entity:
            self.recent_automated[entity] = (now, event.get("new_state"), event.get("attributes_json", {}))
        if event["source"] == "manual" and entity in self.recent_automated:
            ats, automated_state, automated_attrs = self.recent_automated[entity]
            if 0 <= (now - ats).total_seconds() <= self.correction_window and (automated_state != event.get("new_state") or automated_attrs != event.get("attributes_json", {})):
                event["event_type"] = "automation_correction"
                event["category"] = "automation_correction"
                event["metadata_json"] = {"automated_state": automated_state, "automated_attributes": automated_attrs,
                                           "manual_state": event.get("new_state"), "seconds_after": int((now-ats).total_seconds())}
        return self.db.insert_event(event)

    def _flush_one(self):
        entity, point = next(iter(self.environment.items()))
        base = point["event"].copy()
        base.update({"event_type": "environment_aggregate", "source": "aggregate",
                     "old_state": None, "new_state": str(point["last"]),
                     "metadata_json": {"count": point["count"], "avg": point["total"]/point["count"],
                                       "min": point["min"], "max": point["max"], "delta": point["last"]-point["first"]}})
        self.db.insert_event(base)
        del self.environment[entity]  # retain the sample if insertion fails

    def flush_environment(self):
        count = len(self.environment)
        if count:
            # Caller can wrap the entire replay in Database.batch().
            while self.environment:
                self._flush_one()
        return count
