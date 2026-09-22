import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from .llm_schema import RetryableModelFormatError, validate_question_response
from .question_policy import QuestionPolicy, default as default_policy
from .time_display import chinese_label, moment
logger = logging.getLogger(__name__)
ANSWER_SCOPES = {'all': 'all', 'period': 'period', 'daily': 'daily', 'weekly': 'weekly', 'slot': None}
ANSWER_SCOPE_LABELS = {'all': '所有报告', 'period': '所有六小时报告', 'daily': '日报', 'weekly': '周报', 'slot': '下一天的同样时段'}
DUPLICATE_SIMILARITY = 0.57
HISTORY_LIMIT = 60
MODEL_HISTORY_LIMIT = 6
JOB_STATE_KEY = 'question_job_status'
QUEUE_STATE_KEY = 'question_queue'
QUEUE_LIMIT = 20
DEFAULT_PEAK_WINDOWS = '09:00-12:00,14:00-18:00'
DEFAULT_PEAK_WEEKDAYS = '1,2,3,4,5'
EMPTY_RETRY_INSTRUCTION = '\n\n注意：本轮报告里存在值得确认的现象（见 report_supports、report_noteworthy、report_anomalies）。请先找出其中最需要房主确认的 1~2 件事，再判断是否与 recent_questions 重复；只有当确实存在答案能改变后续分析的问题时才返回空数组。'
RETRY_SUMMARY_CHARS = 300

class QuestionAgent:

    def __init__(self, database, model_client, timeout_path=str(Path(__file__).parent / 'resources/prompts/questions.txt'), policy=None, settings=None):
        self.db = database
        self.model = model_client
        self.prompt = Path(timeout_path).read_text(encoding='utf-8')
        self.policy = policy if policy is not None else default_policy()
        self.question_model = str(getattr(settings, 'question_model', '') or '')
        self.offpeak_only = bool(getattr(settings, 'question_offpeak_only', False))
        self.peak_windows = self._parse_windows(getattr(settings, 'question_peak_windows', DEFAULT_PEAK_WINDOWS))
        self.peak_weekdays = self._parse_weekdays(getattr(settings, 'question_peak_weekdays', DEFAULT_PEAK_WEEKDAYS))
        self.tz = ZoneInfo(str(getattr(settings, 'timezone', 'Asia/Shanghai')))

    @staticmethod
    def _parse_windows(value):
        windows = []
        for chunk in str(value or '').split(','):
            start, _, end = chunk.strip().partition('-')
            try:
                start_hour, start_minute = (int(part) for part in start.split(':'))
                end_hour, end_minute = (int(part) for part in end.split(':'))
            except (TypeError, ValueError):
                continue
            windows.append((start_hour * 60 + start_minute, end_hour * 60 + end_minute))
        return windows

    @staticmethod
    def _parse_weekdays(value):
        days = set()
        for chunk in str(value or '').split(','):
            try:
                days.add(int(chunk.strip()))
            except (TypeError, ValueError):
                continue
        return days

    def peak_now(self, now=None):
        moment_now = now or datetime.now(self.tz)
        if moment_now.tzinfo is None:
            moment_now = moment_now.replace(tzinfo=self.tz)
        if self.peak_weekdays and moment_now.isoweekday() not in self.peak_weekdays:
            return False
        minute_of_day = moment_now.hour * 60 + moment_now.minute
        return any((start <= minute_of_day < end for start, end in self.peak_windows))

    def defer(self, scope, key):
        if not key:
            return False
        queue = self.queued()
        if any((entry.get('scope') == scope and entry.get('key') == key for entry in queue)):
            return False
        queue.append({'scope': scope, 'key': str(key)})
        self.db.set_state(QUEUE_STATE_KEY, json.dumps(queue[-QUEUE_LIMIT:], ensure_ascii=False))
        return True

    def queued(self):
        raw = self.db.scalar('SELECT value FROM system_state WHERE key=?', (QUEUE_STATE_KEY,))
        try:
            parsed = json.loads(raw or '[]')
        except (TypeError, ValueError):
            return []
        return [entry for entry in parsed if isinstance(entry, dict)] if isinstance(parsed, list) else []

    def take_queued(self, limit=4):
        queue = self.queued()
        taken, remaining = (queue[:limit], queue[limit:])
        self.db.set_state(QUEUE_STATE_KEY, json.dumps(remaining, ensure_ascii=False) if remaining else '[]')
        return [(entry.get('scope') or 'period', entry.get('key')) for entry in taken]

    def report_for(self, scope, key):
        table, column = ('period_summaries', 'period_start') if scope == 'period' else ('daily_summaries', 'day') if scope == 'daily' else ('weekly_summaries', 'week_start')
        rows = self.db.rows(f'SELECT summary_json FROM {table} WHERE {column}=? LIMIT 1', (str(key),))
        if not rows:
            return None
        try:
            stored = json.loads(rows[0]['summary_json'] or '{}')
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(stored, dict) or stored.get('generation_status') != 'succeeded':
            return None
        return stored

    def pending(self, limit=20):
        return self._rows("SELECT * FROM ai_questions WHERE status='pending' ORDER BY id DESC LIMIT ?", (limit,))

    def answered(self, limit=50):
        return self._rows("SELECT * FROM ai_questions WHERE status='answered' ORDER BY id DESC LIMIT ?", (limit,))

    def _rows(self, sql, params=()):
        rows = self.db.rows(sql, params)
        for row in rows:
            row['scope_label'] = ANSWER_SCOPE_LABELS.get(row.get('answer_scope') or '', None)
            row['target_label'] = moment(row.get('answer_target'))
            row['created_label'] = moment(row.get('created_at'))
            row['answered_label'] = moment(row.get('answered_at'))
            row['source_label'] = self._source_label(row)
            row['slot_available'] = self._next_slot(row.get('scope_key')) is not None
        return rows

    @staticmethod
    def _source_label(row):
        key = row.get('scope_key')
        if not key:
            return '你手动发问' if row.get('source') == 'manual' else '报告'
        stamp = chinese_label(key)
        return f'{stamp} 的报告' if stamp else '报告'

    def answer(self, question_id, text, scope, model=None):
        content = (text or '').strip()
        if not content:
            raise ValueError('回答不能为空')
        if scope not in ANSWER_SCOPES:
            raise ValueError('作用范围无效')
        rows = self.db.rows('SELECT * FROM ai_questions WHERE id=?', (question_id,))
        if not rows:
            raise ValueError('未找到该问题')
        question = rows[0]
        if question['status'] != 'pending':
            raise ValueError('该问题已经回答过')
        target = None
        if scope == 'slot':
            target = self._next_slot(question.get('scope_key'))
            if target is None:
                raise ValueError('该问题不是来自时段报告，无法限定到具体时段')
        guidance_id = None
        if ANSWER_SCOPES[scope] is not None:
            with self.db.connect() as con:
                cursor = con.execute('INSERT INTO ai_guidance(content,scope,source,created_at) VALUES(?,?,?,?)', (self._as_owner_preference(question, content), ANSWER_SCOPES[scope], 'owner', datetime.now(timezone.utc).isoformat()))
                guidance_id = cursor.lastrowid
        with self.db.connect() as con:
            con.execute("UPDATE ai_questions SET status='answered',answer=?,answer_scope=?,answer_target=?,guidance_id=?,answered_at=? WHERE id=?", (content, scope, target, guidance_id, datetime.now(timezone.utc).isoformat(), question_id))
        logger.info('question answered scope=%s guidance=%s', scope, bool(guidance_id))
        return {'id': question_id, 'status': 'answered', 'scope': scope, 'target': target, 'guidance_id': guidance_id}

    @staticmethod
    def _as_owner_preference(question, answer):
        return f"{question['question']}\n房主回答：{answer}"[:1000]

    @staticmethod
    def _next_slot(scope_key):
        if not scope_key or 'T' not in str(scope_key):
            return None
        try:
            parsed = datetime.fromisoformat(str(scope_key))
        except (TypeError, ValueError):
            return None
        if parsed.hour % 6 or parsed.minute or parsed.second:
            return None
        return (parsed + timedelta(days=1)).isoformat()

    def slot_answers(self, period_start):
        rows = self.db.rows("SELECT question,answer FROM ai_questions WHERE status='answered' AND answer_scope='slot' AND answer_target=? ORDER BY id", (period_start,))
        return [{'id': None, 'scope': 'period', 'source': 'owner', 'content': f"{row['question']}\n房主回答：{row['answer']}"[:1000], 'created_at': None} for row in rows]

    def skip(self, question_id):
        with self.db.connect() as con:
            cursor = con.execute("UPDATE ai_questions SET status='skipped',answered_at=? WHERE id=? AND status='pending'", (datetime.now(timezone.utc).isoformat(), question_id))
        if cursor.rowcount != 1:
            raise ValueError('未找到待回答的问题')
        return {'id': question_id, 'status': 'skipped'}
    _NUMERALS = str.maketrans({'〇': '0', '零': '0', '一': '1', '二': '2', '两': '2', '三': '3', '四': '4', '五': '5', '六': '6', '七': '7', '八': '8', '九': '9', '十': '1', '百': '1', '千': '1', '万': '1', '点': '', '半': ''})

    @classmethod
    def _normalized(cls, text):
        translated = str(text or '').translate(cls._NUMERALS)
        return ''.join((ch.lower() for ch in translated if ch.isalnum() and (not ch.isdigit())))

    @staticmethod
    def _bigrams(text):
        return {text[index:index + 2] for index in range(max(0, len(text) - 1))}

    @classmethod
    def _similarity(cls, one, other):
        left, right = (cls._bigrams(one), cls._bigrams(other))
        if not left or not right:
            return 0.0
        return len(left & right) / min(len(left), len(right))

    def history(self, limit=HISTORY_LIMIT):
        return self.db.rows('SELECT id,question,status,answer,source,scope_key,created_at FROM ai_questions ORDER BY id DESC LIMIT ?', (limit,))

    @classmethod
    def _duplicate_of(cls, question, candidates):
        normalized = cls._normalized(question)
        if len(normalized) < 6:
            return None
        for candidate in candidates:
            other = cls._normalized(candidate)
            if not other:
                continue
            if normalized == other:
                return candidate
            if len(normalized) >= 10 and (normalized in other or other in normalized):
                return candidate
            if cls._similarity(normalized, other) >= DUPLICATE_SIMILARITY:
                return candidate
        return None

    def _recent_for_model(self, limit=MODEL_HISTORY_LIMIT):
        rows = self.history(limit)
        result = []
        for row in reversed(rows):
            entry = {'question': str(row.get('question') or '')[:120], 'status': row.get('status')}
            answer = str(row.get('answer') or '').strip()
            if answer:
                entry['owner_answer'] = answer[:80]
            result.append(entry)
        return result

    def _signal_notes(self, report):
        if not self.policy:
            return []
        digest = ' '.join((str(report.get(key) or '') for key in ('summary', 'supports', 'noteworthy', 'anomalies')))
        try:
            return self.policy.sparse_notes(digest)
        except Exception as error:
            logger.warning('signal notes unavailable error=%s', type(error).__name__)
            return []

    def _record_job(self, source, proposed, stored, dropped):
        state = {'status': 'succeeded', 'source': source, 'proposed': proposed, 'stored': stored, 'retried': bool(dropped.get('retried')), 'duplicate_dropped': dropped.get('duplicate', 0), 'artifact_dropped': dropped.get('artifact', 0), 'policy_dropped': dropped.get('policy', 0), 'quota_dropped': dropped.get('quota', 0), 'empty_dropped': dropped.get('empty', 0), 'finished_at': datetime.now(timezone.utc).isoformat()}
        try:
            self.db.set_state(JOB_STATE_KEY, json.dumps(state, ensure_ascii=False, separators=(',', ':')))
        except Exception as error:
            logger.error('question job state write failed error=%s', type(error).__name__)

    def payload(self, report, scope, scope_key):
        stored = report if isinstance(report, dict) else {}
        return {'scope': scope, 'period_start': scope_key, 'timezone': 'Asia/Shanghai (UTC+08:00, Beijing Time)', 'report_summary': str(stored.get('summary') or '')[:2000], 'report_supports': (stored.get('supports') or [])[:3], 'report_noteworthy': (stored.get('noteworthy') or [])[:3], 'report_anomalies': (stored.get('anomalies') or [])[:3], 'recent_questions': self._recent_for_model(), 'signal_notes': self._signal_notes(stored)}

    async def propose(self, report, scope, scope_key, model=None, source='report'):
        model = self.question_model or model
        payload = self.payload(report, scope, scope_key)
        ask = getattr(self.model, 'analyze_questions', None) or self.model.analyze

        async def ask_items(prompt):
            try:
                response = await ask(prompt, payload, model=model) if model else await ask(prompt, payload)
                result = response[0] if isinstance(response, tuple) else response
            except RetryableModelFormatError as error:
                logger.warning('question proposal rejected error=%s', type(error).__name__)
                return None
            except Exception as error:
                logger.warning('question proposal failed error=%s', type(error).__name__)
                return None
            try:
                return result if isinstance(result, list) else validate_question_response(result)
            except RetryableModelFormatError as error:
                logger.warning('question proposal rejected error=%s', type(error).__name__)
                return None
        items = await ask_items(self.prompt)
        retried = False
        if items == [] and self._worth_retrying(report):
            logger.info('no question proposed; retrying once with a nudge source=%s', source)
            items = await ask_items(self.prompt + EMPTY_RETRY_INSTRUCTION)
            retried = True
        if not items:
            self._record_job(source, 0, 0, {'retried': int(retried)})
            return []
        created = self.store(items, source, scope_key, retried=retried)
        if retried:
            logger.info('question retry produced count=%s', len(created))
        return created

    @staticmethod
    def _worth_retrying(report):
        stored = report if isinstance(report, dict) else {}
        for key in ('supports', 'noteworthy', 'anomalies'):
            if stored.get(key):
                return True
        return len(str(stored.get('summary') or '')) >= RETRY_SUMMARY_CHARS
    _SCOPE_WORDS = ('触发', '刷新', '巡检')
    _COUNT_PATTERN = re.compile('\\d+\\s*次')

    @classmethod
    def _is_scope_artifact(cls, question, why):
        for text in (question, why):
            if any((word in text for word in cls._SCOPE_WORDS)) and cls._COUNT_PATTERN.search(text):
                return True
        return False

    def store(self, items, source, scope_key, retried=False):
        created = []
        dropped = {'duplicate': 0, 'artifact': 0, 'empty': 0, 'policy': 0, 'quota': 0, 'retried': int(bool(retried))}
        history = self.history()
        candidates = [row.get('question') for row in history]
        accepted = []
        quota_pool = [row.get('question') for row in history]
        now = datetime.now(timezone.utc).isoformat()
        with self.db.connect() as con:
            for item in items[:3]:
                question = str(item.get('question') or '').strip()
                if not question:
                    dropped['empty'] += 1
                    continue
                why = str(item.get('why') or '')[:300]
                if self._is_scope_artifact(question[:300], why):
                    dropped['artifact'] += 1
                    logger.warning('question rejected as scope artifact source=%s', source)
                    continue
                policy_key = self.policy.forbidden_reason(question, why) if self.policy else None
                if policy_key:
                    dropped['policy'] += 1
                    logger.warning('question rejected by policy key=%s source=%s', policy_key, source)
                    continue
                repeated = self._duplicate_of(question, candidates) is not None or self._duplicate_of(question, accepted) is not None
                if repeated:
                    dropped['duplicate'] += 1
                    logger.warning('question rejected as duplicate source=%s known=%s', source, len(candidates))
                    continue
                quota_key = self.policy.quota_reason(question, quota_pool + accepted) if self.policy else None
                if quota_key:
                    dropped['quota'] += 1
                    logger.warning('question rejected by quota key=%s source=%s', quota_key, source)
                    continue
                cursor = con.execute("INSERT INTO ai_questions(question,why,source,scope_key,status,created_at) VALUES(?,?,?,?,'pending',?)", (question[:300], why, source, scope_key, now))
                created.append(cursor.lastrowid)
                accepted.append(question)
        self._record_job(source, min(len(items), 3), len(created), dropped)
        if created:
            logger.info('question proposal stored count=%s source=%s', len(created), source)
        return created

    def latest_report_key(self, scope='period'):
        table, key = ('period_summaries', 'period_start') if scope == 'period' else ('daily_summaries', 'day') if scope == 'daily' else ('weekly_summaries', 'week_start')
        rows = self.db.rows(f'SELECT {key} AS report_key,summary_json FROM {table} ORDER BY {key} DESC LIMIT 20')
        for row in rows:
            try:
                stored = json.loads(row['summary_json'])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(stored, dict) and stored.get('generation_status') == 'succeeded':
                return (row['report_key'], stored, scope)
        return (None, None, scope)

    async def propose_from_latest(self, scope='period', model=None):
        model = self.question_model or model
        report_key, report, scope = self.latest_report_key(scope)
        if report is None:
            logger.info('no successful report to base questions on')
            return []
        return await self.propose(report, scope, report_key, model, source='manual')
