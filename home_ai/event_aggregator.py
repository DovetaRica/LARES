from collections import defaultdict
from datetime import datetime, timezone


class EventAggregator:
    def __init__(self, database, correction_window_seconds=180):
        self.db = database
        self.environment = defaultdict(list)
        self.recent_automated = {}
        self.correction_window = correction_window_seconds

    def process(self, event):
        if event["category"] == "environment":
            try:
                self.environment[event["entity_id"]].append((event["ts"], float(event["new_state"]), event))
            except (TypeError, ValueError):
                pass
            return None
        now = datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))
        entity = event.get("entity_id")
        if event["source"] == "automation" and entity:
            self.recent_automated[entity] = (now, event.get("new_state"), event.get("attributes_json", {}))
        if event["source"] == "manual" and entity in self.recent_automated:
            ats, automated_state, automated_attrs = self.recent_automated[entity]
            if 0 <= (now - ats).total_seconds() <= self.correction_window:
                event["event_type"] = "automation_correction"
                event["category"] = "automation_correction"
                event["metadata_json"] = {"automated_state": automated_state, "automated_attributes": automated_attrs,
                                           "manual_state": event.get("new_state"), "seconds_after": int((now-ats).total_seconds())}
        return self.db.insert_event(event)

    def flush_environment(self):
        count = 0
        for entity, points in list(self.environment.items()):
            if not points:
                continue
            values = [p[1] for p in points]
            base = points[-1][2].copy()
            base.update({"ts": points[-1][0], "event_type": "environment_aggregate", "source": "aggregate",
                         "old_state": None, "new_state": str(values[-1]),
                         "metadata_json": {"count": len(values), "avg": sum(values)/len(values), "min": min(values),
                                           "max": max(values), "delta": values[-1]-values[0]}})
            self.db.insert_event(base)
            count += 1
        self.environment.clear()
        return count

