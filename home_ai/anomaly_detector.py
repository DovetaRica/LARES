from datetime import datetime, timedelta, timezone


class AnomalyDetector:
    """Conservative, deterministic signals; it never sends notifications itself."""

    def __init__(self, database):
        self.db = database

    def detect(self, now=None):
        now = now or datetime.now(timezone.utc)
        findings = []
        correction_since = (now - timedelta(days=14)).isoformat()
        corrections = self.db.rows(
            "SELECT entity_id,COUNT(*) samples,MIN(ts) first_seen,MAX(ts) last_seen "
            "FROM events WHERE event_type='automation_correction' AND ts>=? "
            "GROUP BY entity_id HAVING COUNT(*)>=3", (correction_since,))
        for row in corrections:
            findings.append({"type": "repeated_automation_correction", "entity_id": row["entity_id"],
                             "sample_count": row["samples"], "first_seen": row["first_seen"],
                             "last_seen": row["last_seen"], "severity": "high"})

        stale_before = (now - timedelta(hours=24)).isoformat()
        stale = self.db.rows(
            "SELECT entity_id,MAX(ts) last_seen,COUNT(*) samples FROM events "
            "WHERE category='environment' GROUP BY entity_id HAVING COUNT(*)>=4 AND MAX(ts)<?", (stale_before,))
        for row in stale:
            findings.append({"type": "sensor_stale", "entity_id": row["entity_id"],
                             "sample_count": row["samples"], "last_seen": row["last_seen"], "severity": "medium"})
        return findings

