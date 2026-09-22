import hashlib
import json
import logging
import math
import re
from datetime import datetime, timezone
logger = logging.getLogger(__name__)
MAX_COUNT = 1000000
TYPE_RE = re.compile('^[A-Za-z0-9_-]{1,64}$')

def calculate_confidence(sample_count, consistency, counter_examples=0, span_days=1, recency_days=0, confirmed=False):
    if confirmed:
        return 1.0
    sample_factor = min(1.0, math.log1p(max(0, sample_count)) / math.log(21))
    adjusted_consistency = max(0.0, min(1.0, consistency)) * (sample_count / max(1, sample_count + counter_examples))
    span_factor = min(1.0, max(0.25, span_days / 14))
    recency = math.exp(-max(0, recency_days) / 30)
    return round(sample_factor * adjusted_consistency * (0.7 + 0.3 * span_factor) * recency, 4)

class MemoryManager:

    def __init__(self, database, min_pattern_samples=5):
        self.db = database
        if isinstance(min_pattern_samples, bool) or not isinstance(min_pattern_samples, int) or (not 1 <= min_pattern_samples <= 100):
            raise ValueError('invalid min_pattern_samples')
        self.min_pattern_samples = min_pattern_samples

    @staticmethod
    def _count(value, field):
        if isinstance(value, bool):
            return 0
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        if not math.isfinite(number) or number < 0:
            return 0
        return min(int(number), MAX_COUNT)

    @staticmethod
    def _ratio(value, field):
        if isinstance(value, bool):
            return 0.0
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if not math.isfinite(number):
            return 0.0
        return max(0.0, min(1.0, number))

    @staticmethod
    def _text_hash(value):
        return hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]

    def _normalize(self, item, findings_by_id, evidence_by_id):
        if not isinstance(item, dict):
            raise ValueError('candidate must be a mapping')
        pattern_type = str(item.get('type', 'habit')).strip().lower()
        if not TYPE_RE.fullmatch(pattern_type):
            pattern_type = 'habit'
        description = str(item.get('description', '')).strip()[:500]
        if not description:
            raise ValueError('invalid description')
        reason = str(item.get('reason', '')).strip()[:500]
        evidence = item.get('evidence', [])
        if not isinstance(evidence, list):
            evidence = []
        if pattern_type == 'cooccurrence':
            finding_id = str(item.get('finding_id', '')).strip()
            finding = findings_by_id.get(finding_id)
            if not finding or finding.get('finding_id') != finding_id:
                raise ValueError('invalid finding_id')
            if finding.get('algorithm_version') != 'cooccurrence-v1' or finding.get('status') != 'reported':
                raise ValueError('invalid cooccurrence finding')
            samples = self._count(finding.get('episodes_total'), 'sample_count')
            hits = self._count(finding.get('episodes_with_edge'), 'episodes_with_edge')
            if hits > samples:
                raise ValueError('invalid episodes_with_edge')
            consistency = hits / samples if samples else 0.0
            counters = samples - hits
            span_days = max(1, self._count(finding.get('episode_local_days'), 'span_days'))
            fingerprint = hashlib.sha256(f'cooccurrence|{finding_id}'.encode('utf-8')).hexdigest()[:24]
            evidence = [finding]
        else:
            evidence_ids = item.get('evidence_ids')
            if not isinstance(evidence_ids, list) or not evidence_ids or len(evidence_ids) > 8:
                raise ValueError('ordinary candidate requires evidence_ids')
            if len(evidence_ids) != len(set(evidence_ids)):
                raise ValueError('duplicate evidence_ids')
            evidence = [evidence_by_id.get(str(evidence_id)) for evidence_id in evidence_ids]
            if any((not item or item.get('status') != 'reported' for item in evidence)):
                raise ValueError('invalid evidence_id')
            counts = [self._count(item.get('sample_count'), 'sample_count') for item in evidence]
            days = [max(1, self._count(item.get('local_days'), 'local_days')) for item in evidence]
            if not counts or any((count <= 0 for count in counts)):
                raise ValueError('invalid evidence counts')
            samples = min(counts)
            maximum = max(counts)
            consistency = samples / maximum
            counters = maximum - samples
            span_days = min(days)
            semantic_keys = sorted((str(item.get('semantic_key', '')) for item in evidence))
            if any((not key for key in semantic_keys)):
                raise ValueError('invalid evidence semantic key')
            fingerprint = hashlib.sha256(f"evidence-v1|{pattern_type}|{'|'.join(semantic_keys)}".encode('utf-8')).hexdigest()[:24]
        return {'type': pattern_type, 'description': description, 'reason': reason, 'evidence': evidence, 'sample_count': samples, 'consistency': consistency, 'counter_examples': counters, 'span_days': span_days, 'fingerprint': fingerprint}

    def upsert_candidates(self, candidates, findings_by_id=None, evidence_by_id=None):
        if not isinstance(candidates, list):
            logger.warning('candidate batch rejected type=%s', type(candidates).__name__)
            return []
        findings_by_id = findings_by_id or {}
        evidence_by_id = evidence_by_id or {}
        now = datetime.now(timezone.utc).isoformat()
        saved = []
        with self.db.connect() as con:
            for raw in candidates:
                raw_type = str(raw.get('type', 'habit')).strip().lower() if isinstance(raw, dict) else 'habit'
                pattern_type = raw_type if TYPE_RE.fullmatch(raw_type) else 'habit'
                description = str(raw.get('description', '')) if isinstance(raw, dict) else ''
                normalized_samples = self._count(raw.get('sample_count', 0), 'sample_count') if isinstance(raw, dict) else 0
                try:
                    item = self._normalize(raw, findings_by_id, evidence_by_id)
                except (TypeError, ValueError, OverflowError) as error:
                    logger.warning('candidate skipped type=%s samples=%s description_sha256=%s error=%s', pattern_type, normalized_samples, self._text_hash(description), type(error).__name__)
                    continue
                samples = item['sample_count']
                if samples < self.min_pattern_samples:
                    logger.warning('candidate below sample floor type=%s samples=%s description_sha256=%s', item['type'], samples, self._text_hash(item['description']))
                    continue
                confidence = calculate_confidence(samples, item['consistency'], item['counter_examples'], item['span_days'])
                existing = con.execute('SELECT * FROM patterns WHERE fingerprint=?', (item['fingerprint'],)).fetchone()
                if existing and existing['status'] in {'rejected', 'confirmed'}:
                    continue
                if existing and existing['status'] == 'confirmed':
                    logger.info('confirmed pattern preserved id=%s', existing['id'])
                    continue
                if existing:
                    con.execute('UPDATE patterns SET description=?,confidence=?,sample_count=?,consistency=?,counter_examples=?,last_seen=?,evidence_json=?,reason=? WHERE id=?', (item['description'], confidence, samples, item['consistency'], item['counter_examples'], now, json.dumps(item['evidence'], ensure_ascii=False), item['reason'], existing['id']))
                    saved.append(existing['id'])
                else:
                    cur = con.execute("INSERT INTO patterns(type,fingerprint,description,confidence,sample_count,consistency,counter_examples,first_seen,last_seen,status,evidence_json,reason) VALUES(?,?,?,?,?,?,?,?,?,'candidate',?,?)", (item['type'], item['fingerprint'], item['description'], confidence, samples, item['consistency'], item['counter_examples'], now, now, json.dumps(item['evidence'], ensure_ascii=False), item['reason']))
                    saved.append(cur.lastrowid)
        return saved

    def feedback(self, pattern_id, action, note=None):
        if action not in {'confirm', 'reject'}:
            raise ValueError('invalid action')
        now = datetime.now(timezone.utc).isoformat()
        status = 'confirmed' if action == 'confirm' else 'rejected'
        with self.db.connect() as con:
            pattern = con.execute('SELECT * FROM patterns WHERE id=?', (pattern_id,)).fetchone()
            if not pattern:
                return False
            con.execute('UPDATE patterns SET status=?,confidence=? WHERE id=?', (status, 1.0 if status == 'confirmed' else pattern['confidence'], pattern_id))
            con.execute('INSERT INTO feedback(pattern_id,action,note,created_at) VALUES(?,?,?,?)', (pattern_id, action, note, now))
            con.execute('INSERT INTO memories(pattern_id,type,content,confidence,sample_count,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)', (pattern_id, pattern['type'], pattern['description'], 1.0 if status == 'confirmed' else pattern['confidence'], pattern['sample_count'], status, now, now))
        return True
