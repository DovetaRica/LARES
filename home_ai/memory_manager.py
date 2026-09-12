import hashlib
import json
import math
from datetime import datetime, timezone


def calculate_confidence(sample_count, consistency, counter_examples=0, span_days=1, recency_days=0, confirmed=False):
    if confirmed:
        return 1.0
    sample_factor = min(1.0, math.log1p(max(0, sample_count)) / math.log(21))
    adjusted_consistency = max(0.0, min(1.0, consistency)) * (sample_count / max(1, sample_count + counter_examples))
    span_factor = min(1.0, max(0.25, span_days / 14))
    recency = math.exp(-max(0, recency_days) / 30)
    return round(sample_factor * adjusted_consistency * (0.7 + 0.3 * span_factor) * recency, 4)


class MemoryManager:
    def __init__(self, database):
        self.db = database

    def upsert_candidates(self, candidates):
        now = datetime.now(timezone.utc).isoformat()
        saved = []
        with self.db.connect() as con:
            for item in candidates:
                desc = str(item.get("description", "")).strip()
                if not desc:
                    continue
                fingerprint = hashlib.sha256((str(item.get("type"))+"|"+desc.lower()).encode()).hexdigest()[:24]
                samples = int(item.get("sample_count", 0))
                consistency = float(item.get("consistency", 0))
                counters = int(item.get("counter_examples", 0))
                confidence = calculate_confidence(samples, consistency, counters)
                existing = con.execute("SELECT * FROM patterns WHERE fingerprint=?", (fingerprint,)).fetchone()
                if existing and existing["status"] == "rejected":
                    continue
                if existing:
                    con.execute("UPDATE patterns SET confidence=?,sample_count=?,consistency=?,counter_examples=?,last_seen=?,evidence_json=?,reason=? WHERE id=?",
                                (confidence, samples, consistency, counters, now, json.dumps(item.get("evidence",[]),ensure_ascii=False), item.get("reason",""), existing["id"]))
                    saved.append(existing["id"])
                else:
                    cur = con.execute("INSERT INTO patterns(type,fingerprint,description,confidence,sample_count,consistency,counter_examples,first_seen,last_seen,status,evidence_json,reason) VALUES(?,?,?,?,?,?,?,?,?,'candidate',?,?)",
                                      (item.get("type","habit"), fingerprint, desc, confidence, samples, consistency, counters, now, now, json.dumps(item.get("evidence",[]),ensure_ascii=False), item.get("reason","")))
                    saved.append(cur.lastrowid)
        return saved

    def feedback(self, pattern_id, action, note=None):
        if action not in {"confirm", "reject"}:
            raise ValueError("invalid action")
        now = datetime.now(timezone.utc).isoformat()
        status = "confirmed" if action == "confirm" else "rejected"
        with self.db.connect() as con:
            pattern = con.execute("SELECT * FROM patterns WHERE id=?", (pattern_id,)).fetchone()
            if not pattern:
                return False
            con.execute("UPDATE patterns SET status=?,confidence=? WHERE id=?", (status, 1.0 if status == "confirmed" else pattern["confidence"], pattern_id))
            con.execute("INSERT INTO feedback(pattern_id,action,note,created_at) VALUES(?,?,?,?)", (pattern_id, action, note, now))
            con.execute("INSERT INTO memories(pattern_id,type,content,confidence,sample_count,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (pattern_id, pattern["type"], pattern["description"], 1.0 if status == "confirmed" else pattern["confidence"], pattern["sample_count"], status, now, now))
        return True

