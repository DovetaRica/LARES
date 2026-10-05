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

    def __init__(self, database, min_pattern_samples=5, noise_entities=(), non_occupancy_entities=()):
        self.db = database
        self.noise_entities = frozenset(noise_entities)
        self.non_occupancy_entities = frozenset(non_occupancy_entities)
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

    def harvest_session_habits(self, lookback_days=21, min_days=3, min_minutes_per_day=20, now=None):
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        from .report_evidence import trusted_session_rows, clipped_sessions
        from .time_display import chinese_label
        now = now or datetime.now(timezone.utc)
        tz = ZoneInfo('Asia/Shanghai')
        end = now.astimezone(tz)
        start = end - timedelta(days=int(lookback_days))
        rows = trusted_session_rows(self.db, start, end, self.noise_entities, self.non_occupancy_entities)
        totals = {}
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        while day < end:
            day_rows = clipped_sessions(rows, max(day, start), min(day + timedelta(days=1), end))
            rooms = {}
            for row in day_rows:
                if row['type'] not in ('room_activity', 'presence', 'pass_by'):
                    continue
                value = rooms.setdefault(row['room'], [0, 0])
                value[0] += row['duration_seconds']
                value[1] += 1
            for room, (seconds, count) in rooms.items():
                if seconds < min_minutes_per_day * 60:
                    continue
                entry = totals.setdefault(room, {'days': 0, 'seconds': 0, 'sessions': 0})
                entry['days'] += 1
                entry['seconds'] += seconds
                entry['sessions'] += count
            day += timedelta(days=1)
        eligible = {room: item for room, item in totals.items() if item['days'] >= min_days}
        stamp = now.astimezone(timezone.utc).isoformat()
        created, suggestions = ([], [])
        with self.db.connect() as con:
            existing = list(con.execute("SELECT * FROM memories WHERE type='room_habit' ORDER BY id"))
            by_room = {}
            for row in existing:
                match = re.search('「([^」]+)」', row['content'])
                if match:
                    by_room.setdefault(match[1], []).append(row)
            rejected = {room for room, values in by_room.items() if any((row['status'] == 'rejected' for row in values))}
            for room, values in by_room.items():
                if room not in eligible:
                    for row in values:
                        if row['status'] != 'rejected':
                            con.execute("UPDATE memories SET status='obsolete',updated_at=? WHERE id=?", (stamp, row['id']))
            active = []
            for room, item in sorted(eligible.items(), key=lambda pair: -pair[1]['seconds']):
                if room in rejected:
                    continue
                content = f"程序观察（未经房主确认）：「{room}」在 {chinese_label(start.isoformat())} 至 {chinese_label(end.isoformat())} 的北京日中，有 {item['days']} 天占用不少于 {min_minutes_per_day} 分钟；可信区间合计 {round(item['seconds'] / 3600, 1)} 小时。已截窗并合并同房间重叠，不推断人员身份或因果。"
                confidence = min(0.95, 0.55 + 0.05 * item['days'])
                old = by_room.get(room, [])
                if old:
                    con.execute("UPDATE memories SET content=?,confidence=?,sample_count=?,status='observed',updated_at=? WHERE id=?", (content, confidence, item['days'], stamp, old[-1]['id']))
                    for stale in old[:-1]:
                        con.execute("UPDATE memories SET status='obsolete',updated_at=? WHERE id=?", (stamp, stale['id']))
                elif len(created) < 2:
                    cur = con.execute("INSERT INTO memories(pattern_id,type,content,confidence,sample_count,status,created_at,updated_at) VALUES(NULL,'room_habit',?,?,?,'observed',?,?)", (content, confidence, item['days'], stamp, stamp))
                    created.append((cur.lastrowid, room))
                else:
                    continue
                active.append((room, content, confidence, item['days']))
            for row in con.execute("SELECT * FROM automation_suggestions WHERE pattern_id IS NULL AND status='proposed'").fetchall():
                try:
                    evidence = json.loads(row['evidence_json'])
                    room = next((x.get('room') for x in evidence if x.get('kind') == 'session_habit'))
                except (ValueError, TypeError, StopIteration, AttributeError):
                    continue
                match = next((x for x in active if x[0] == room), None)
                if not match:
                    con.execute("UPDATE automation_suggestions SET status='obsolete' WHERE id=?", (row['id'],))
                else:
                    con.execute('UPDATE automation_suggestions SET description=?,confidence=?,sample_count=?,evidence_json=? WHERE id=?', (match[1], match[2], match[3], json.dumps([{'kind': 'session_habit', 'room': room, 'as_of': stamp}], ensure_ascii=False), row['id']))
            if created:
                room = created[0][1]
                match = next((x for x in active if x[0] == room))
                title = f'复核「{room}」相关自动化是否贴合作息'
                if not con.execute('SELECT 1 FROM automation_suggestions WHERE title=?', (title,)).fetchone():
                    con.execute("INSERT INTO automation_suggestions(pattern_id,title,description,evidence_json,confidence,sample_count,status,created_at) VALUES(NULL,?,?,?,?,?,'proposed',?)", (title, match[1], json.dumps([{'kind': 'session_habit', 'room': room, 'as_of': stamp}], ensure_ascii=False), match[2], match[3], stamp))
                    suggestions.append(title)
        logger.info('session observations refreshed created=%s suggestions=%s', len(created), len(suggestions))
        return (created, suggestions)
