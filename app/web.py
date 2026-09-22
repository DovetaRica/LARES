import asyncio
import json
import logging
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasic, HTTPBasicCredentials, HTTPBearer
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from .behavior_analyzer import PERIOD_NON_TERMINAL
from .question_agent import ANSWER_SCOPE_LABELS
from . import condense
from . import time_display
logger = logging.getLogger(__name__)
_HEALTH_CACHE_SECONDS = 30
_database_ok_cache: dict = {'value': True, 'at': 0.0}
_ollama_ok_cache: dict = {'value': True, 'at': 0.0}
TEMPLATE_DIR = Path(__file__).parent / 'templates'
templates = Jinja2Templates(directory=TEMPLATE_DIR)
STATIC_DIR = Path(__file__).parent / 'static'

def static_version(name):
    try:
        return int((STATIC_DIR / name).stat().st_mtime)
    except OSError:
        return 0
templates.env.globals['static_version'] = static_version
basic = HTTPBasic(auto_error=False)
advisor_bearer = HTTPBearer(auto_error=False)
PATTERN_STATUSES = {'candidate', 'confirmed', 'rejected', 'obsolete'}

class FeedbackBody(BaseModel):
    note: str | None = Field(default=None, max_length=1000)

class LegacyFeedbackBody(FeedbackBody):
    action: str

class GuidanceBody(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    scope: Literal['all', 'period', 'daily', 'weekly'] = 'all'

class RegenerateBody(BaseModel):
    scope: Literal['period', 'daily', 'weekly']
    key: str = Field(min_length=4, max_length=64)
    model: str | None = Field(default=None, max_length=64)

class CondenseRule(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(min_length=1, max_length=1000)
    scope: Literal['all', 'period', 'daily', 'weekly'] = 'all'
    replaces: list[int] = Field(default_factory=list, max_length=200)

class CondenseProposalBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    external_id: str = Field(min_length=3, max_length=100)
    source: str = Field(min_length=1, max_length=50)
    rules: list[CondenseRule] = Field(min_length=1, max_length=40)
    notes: str = Field(default='', max_length=2000)

class DistillBody(BaseModel):
    scope: Literal['period', 'daily', 'weekly'] = 'period'
    key: str = Field(min_length=4, max_length=64)

class MaintainerBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    note: str = Field(default='', max_length=200)

class MaintainerScoreBody(BaseModel):
    target: str = Field(min_length=1, max_length=80)
    scorer: str = Field(min_length=1, max_length=80)
    score: int = Field(ge=0, le=100)
    note: str = Field(default='', max_length=200)
RECENT_ANSWERS = 8
REGENERATION_SOURCES = {'period': ('period_summaries', 'period_start'), 'daily': ('daily_summaries', 'day'), 'weekly': ('weekly_summaries', 'week_start')}

class RegenerationRegistry:

    def __init__(self):
        self.queue = asyncio.Queue()
        self.jobs = []
        self.running = None

    @property
    def in_flight(self):
        return {(job['scope'], job['key']) for job in self.jobs}

    def add(self, scope, key, value, model=None):
        if any((job['scope'] == scope and job['key'] == key for job in self.jobs)):
            return False
        job = {'scope': scope, 'key': key, 'value': value, 'model': model, 'queued_at': datetime.now(timezone.utc).isoformat()}
        self.jobs.append(job)
        self.queue.put_nowait(job)
        return True

    def snapshot(self):
        return {'running': dict(self.running) if self.running else None, 'waiting': [dict(job) for job in self.jobs if job is not self.running]}
REGENERATION_REGISTRY = RegenerationRegistry()

async def run_regeneration_worker(analyzer, registry):
    while True:
        job = await registry.queue.get()
        registry.running = job
        try:
            value, model = (job['value'], job['model'])
            if job['scope'] == 'period':
                await analyzer.period(end=datetime.fromisoformat(str(value)), source='manual', model=model)
            elif job['scope'] == 'daily':
                await analyzer.daily(day=date.fromisoformat(str(value)[:10]), model=model)
            elif job['scope'] == 'questions':
                await analyzer.ask_questions('period', model=model)
            elif job['scope'] == 'distill':
                report_scope, _, report_key = str(job['value']).partition(':')
                await analyzer.distill(report_scope or 'period', report_key)
            else:
                await analyzer.weekly(week_start=date.fromisoformat(str(value)[:10]), model=model)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error('manual report regeneration failed error=%s', type(error).__name__)
        finally:
            registry.running = None
            registry.jobs = [item for item in registry.jobs if item is not job]
            registry.queue.task_done()

class AdvisorSubmissionBody(GuidanceBody):
    external_id: str = Field(min_length=3, max_length=100, pattern='^[A-Za-z0-9][A-Za-z0-9._:-]*$')
    source: str = Field(min_length=1, max_length=50, pattern='^[A-Za-z0-9][A-Za-z0-9._ -]*$')

def _json(value, fallback):
    if value in (None, ''):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value

def _as_timezone(value, tz):
    if not value:
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(tz).isoformat()
    except (TypeError, ValueError):
        return value

def _scored_values(target_scores):
    values = []
    if not isinstance(target_scores, dict):
        return values
    for item in target_scores.values():
        if not isinstance(item, dict):
            continue
        try:
            values.append(max(0, min(100, int(item.get('score')))))
        except (TypeError, ValueError):
            continue
    return values
REPORT_STATUS_LABELS = {'succeeded': ('confirmed', '模型报告已生成'), 'model_output_rejected': ('rejected', '模型输出未通过校验，已落确定性降级报告'), 'generating': ('candidate', '正在生成中，请稍后刷新'), 'interrupted': ('candidate', '生成被中断，下次启动会自动重试'), 'failed': ('rejected', '生成失败'), 'auto_retry_exhausted': ('candidate', '已达自动重试上限，可在页面手动重新总结')}
ISO_OFFSET_RE = re.compile('^(\\d{4}-\\d{2}-\\d{2}[T ]\\d{2}:\\d{2}(?::\\d{2})?(?:\\.\\d+)?) (\\d{2}:?\\d{2})$')
REPORT_DIAGNOSTIC_FIELDS = frozenset({'cooccurrence_meta', 'cooccurrence_findings_full'})

def report_body(summary):
    if not isinstance(summary, dict):
        return summary
    return {key: value for key, value in summary.items() if key not in REPORT_DIAGNOSTIC_FIELDS and value not in (None, '', [], {})}
REPORT_VALUE_LABELS = {'severity': {'high': '高', 'medium': '中', 'low': '低'}, 'type': {'repeated_automation_correction': '自动化被反复纠正', 'sensor_stale': '传感器长时间没有新数据', 'anomaly_detection': '规律性异常', 'user_guidance_action_required': '需要你按偏好判断', 'deterministic_anomaly': '确定性异常（由程序规则发现）', 'cooccurrence': '共现规律'}, 'kind': {'cooccurrence': '共现分析'}, 'classification': {'suspected_robot_interference': '疑似机器人干扰', 'suspected_device_interference': '疑似运动设备干扰', 'undetermined': '无法判断'}, 'status': {'reported': '已达到报告条件', 'needs_observation': '仍需继续观察', 'insufficient_evidence': '证据不足', 'open': '进行中', 'closed': '已结束'}, 'source': {'system': '系统', 'manual': '手动', 'automation': '自动化'}, 'event_type': {'state_changed': '状态发生变化'}}
ENTITY_ID_KEYS = frozenset({'entity_id', 'entity_ids'})
ENTITY_ID_LIKE_RE = re.compile('[a-z][a-z0-9_]*\\.[a-z0-9_]+', re.IGNORECASE)

def looks_like_entity_id(value):
    return bool(ENTITY_ID_LIKE_RE.fullmatch(str(value or '').strip()))
ENTITY_REFERENCE_RE = re.compile('`?((?:automation|binary_sensor|button|camera|climate|cover|device_tracker|event|fan|light|lock|media_player|number|person|scene|script|select|sensor|switch|vacuum)\\.[a-z0-9_]+)`?', re.IGNORECASE)
ISO_IN_TEXT_RE = re.compile('\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}(?::\\d{2})?(?:\\.\\d+)?(?:Z|[+-]\\d{2}:?\\d{2})')
LOCAL_RANGE_IN_TEXT_RE = re.compile('(\\d{4})-(\\d{2})-(\\d{2})(?:[ T](\\d{2}:\\d{2}))?(?:\\s*[–—~-]\\s*|\\s*[至到]\\s*)(\\d{4})-(\\d{2})-(\\d{2})(?:[ T](\\d{2}:\\d{2}))?')
REPORT_TEXT_TERMS = (('deterministic_anomaly', '确定性异常（由程序规则发现）'), ('cooccurrence_findings', '共现分析结果'), ('cooccurrence_meta', '共现分析统计'), ('episodes_valid', '有效清扫时段数'), ('needs_observation', '仍需继续观察'), ('insufficient_evidence', '证据不足'), ('sensor_stale', '传感器长时间没有新数据'), ('state_changed', '状态发生变化'), ('unknown', '未知'), ('unavailable', '不可用'), ('high', '高'), ('medium', '中'), ('low', '低'), ('entity_identity_catalog', '设备身份清单'))

def entity_display(db, entity_id, profile):
    label, area = (None, '')
    rows = db.rows('SELECT attributes_json,area FROM events WHERE entity_id=? ORDER BY ts DESC LIMIT 1', (entity_id,))
    if rows:
        try:
            attributes = json.loads(rows[0].get('attributes_json') or '{}')
        except ValueError:
            attributes = {}
        label = str(attributes.get('friendly_name') or '').strip() or None
        area = str(rows[0].get('area') or '').strip()
    metadata = profile.metadata(entity_id) if profile else {}
    if not label:
        label = str(metadata.get('life_device') or '').strip() or None
    area = area or str(metadata.get('life_area') or '').strip()
    if not label:
        return None
    return f'{area} · {label}' if area else label

def readable_text(value, db=None, profile=None):
    text = str(value)

    def replace_entity(match):
        entity_id = match.group(1)
        label = entity_display(db, entity_id, profile) if db else None
        return label or entity_id
    text = ENTITY_REFERENCE_RE.sub(replace_entity, text)

    def replace_range(match):
        first = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        last = datetime(int(match.group(5)), int(match.group(6)), int(match.group(7)))
        head = f'{first.month}月{first.day}日'
        tail = f'{last.month}月{last.day}日'
        if match.group(4):
            head = f'{head} {match.group(4)}'
        if match.group(8):
            tail = f'次日 {match.group(8)}' if last.date() == first.date() + timedelta(days=1) else f'{tail} {match.group(8)}'
            return f'{head} 至{tail}'
        return f'{head} 至 {tail}'
    text = LOCAL_RANGE_IN_TEXT_RE.sub(replace_range, text)
    text = ISO_IN_TEXT_RE.sub(lambda match: time_display.chinese_label(match.group(0)) or match.group(0), text)
    text = re.sub('deterministic_anomaly_(\\d+)', '确定性异常证据 \\1', text, flags=re.IGNORECASE)
    text = re.sub('cooccurrence_meta\\s*显示\\s*episodes_valid\\s*=\\s*(\\d+)', '共现分析统计显示：有效清扫时段为 \\1 次', text, flags=re.IGNORECASE)
    text = re.sub('cooccurrence_findings\\s*(?:为|是)?\\s*(?:空|empty|\\[\\])', '没有发现可用于分析的共现结果', text, flags=re.IGNORECASE)
    text = text.replace('输入中无扫地机器人相关实体记录', '本时段没有记录到扫地机器人的相关状态')
    text = text.replace('用户关注的', '您关注的')
    text = text.replace('用户关注点中关于', '您关注的')
    for source, label in REPORT_TEXT_TERMS:
        text = re.sub(f'(?<![A-Za-z0-9_]){re.escape(source)}(?![A-Za-z0-9_])', label, text, flags=re.IGNORECASE)
    text = text.replace('设备身份清单 中', '设备身份清单中').replace('属于 证据不足', '属于证据不足')
    text = re.sub('[ \\t]{2,}', ' ', text)
    return text

def readable_report(value, db=None, profile=None, key=None, metadata=False):
    if isinstance(value, dict):
        return {name: readable_report(item, db, profile, name, metadata or name in REPORT_DIAGNOSTIC_FIELDS) for name, item in value.items() if metadata or item not in (None, '', [], {})}
    if isinstance(value, list):
        return [readable_report(item, db, profile, key, metadata) for item in value]
    if isinstance(value, str):
        if metadata:
            return value
        if key in ENTITY_ID_KEYS and '.' in value:
            label = entity_display(db, value, profile) if db else None
            return {'label': label, 'hint': value} if label else value
        translated = REPORT_VALUE_LABELS.get(key or '', {}).get(value)
        return translated if translated is not None else readable_text(value, db, profile)
    return value
REPORT_FIELD_LABELS = {'summary': '正文摘要', 'supports': '支撑观察', 'new_candidates': '候选规律', 'anomalies': '异常', 'automation_corrections': '自动化纠正', 'noteworthy': '值得注意', 'error_type': '错误类型', 'cooccurrence_findings': '共现发现', 'cooccurrence_findings_full': '共现发现（完整记录）', 'cooccurrence_meta': '共现分析元数据', 'reference_model': '参考的范例模型', 'distilled_from': '蒸馏来源模型（学习其分析，事实仍来自本次输入）', 'reference_period': '范例来源时段', 'date': '日期', 'week_start': '周起始日', 'learning_only': '仍在学习模式', 'automation_suggestions': '自动化建议', 'period_summaries': '各时段小结', 'daily_summaries': '各日总结', 'major_sessions': '主要在场时段', 'patterns': '规律', 'existing_patterns': '已有规律', 'user_guidance': '房主提供的分析偏好', 'reviewed_pattern_feedback': '已确认或否定的规律', 'robot_presence_annotations': '运动设备与人体信号复核', 'annotation_id': '复核编号', 'classification': '当前判断', 'observed_at': '信号时间', 'actor': '相关设备', 'sensor': '人体传感器', 'actor_state': '设备状态', 'spatial_evidence': '空间证据', 'human_evidence': '人员证据', 'basis': '判断依据', 'limitation': '判断限制', 'review_prompt': '请你复核', 'type': '类型', 'entity_id': '实体', 'description': '描述', 'reason': '依据', 'evidence_ids': '证据编号', 'finding_id': '共现结论编号', 'actor_device': '相关设备名称', 'actor_area': '相关设备区域', 'actor_label': '相关设备中文名', 'sensor_device': '传感器名称', 'sensor_area': '传感器区域', 'sensor_label': '传感器中文名', 'sample_count': '样本数', 'first_seen': '首次出现', 'last_seen': '最近出现', 'severity': '严重度', 'room': '房间', 'area': '区域', 'device': '设备', 'appliance_type': '设备类型', 'status': '状态', 'kind': '分析类别', 'event_type': '事件类型', 'source': '来源', 'count': '出现次数', 'old_state': '变化前状态', 'new_state': '变化后状态', 'episodes_valid': '有效清扫时段数', 'episodes_total': '清扫时段总数', 'episodes_with_edge': '出现人体活动的清扫时段数', 'edge_local_days': '出现人体活动的天数', 'episode_local_days': '发生清扫的天数', 'counter_examples': '未同时出现的次数', 'consistency': '同时出现比例', 'algorithm_version': '算法版本', 'config_hash': '配置版本', 'payload_collections': '输入内容裁剪统计', 'before': '裁剪前', 'after': '裁剪后'}

def _restore_offset(value):
    if not value or '+' in value:
        return value
    match = ISO_OFFSET_RE.match(value.strip())
    return f'{match.group(1)}+{match.group(2)}' if match else value
LEGACY_TEXT_KEYS = ('text', 'content', 'summary', 'answer')

def _legacy_text(value, depth=0):
    if depth > 3:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        parts = [part for part in (_legacy_text(item, depth + 1) for item in value) if part]
        return '\n\n'.join(parts) or None
    if isinstance(value, dict):
        for key in LEGACY_TEXT_KEYS:
            if key in value:
                text = _legacy_text(value[key], depth + 1)
                if text:
                    return text
    return None

def _legacy_summary(summary):
    if summary.get('type') == 'home_assistant' or 'content' in summary:
        text = _legacy_text(summary.get('content'))
        if text:
            return text
    return _legacy_text(summary.get('answer'))

def _pattern(row, tz=None):
    if not row:
        return None
    item = dict(row)
    item['evidence'] = _json(item.pop('evidence_json', '[]'), [])
    item['confidence_percent'] = round(float(item.get('confidence') or 0) * 100)
    if tz:
        item['first_seen'] = _as_timezone(item.get('first_seen'), tz)
        item['last_seen'] = _as_timezone(item.get('last_seen'), tz)
    return item

def _relative(value):
    if not value:
        return '尚无事件'
    try:
        then = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        seconds = max(0, int((datetime.now(timezone.utc) - then.astimezone(timezone.utc)).total_seconds()))
        if seconds < 60:
            return f'{seconds} 秒前'
        if seconds < 3600:
            return f'{seconds // 60} 分钟前'
        if seconds < 86400:
            return f'{seconds // 3600} 小时前'
        return f'{seconds // 86400} 天前'
    except (TypeError, ValueError):
        return str(value)

def create_web_router(db, memory, ha, ollama, event_filter, settings, cooccurrence=None, analyzer=None, registry=None, provider_settings=None, questions=None):
    router = APIRouter()
    local_tz = ZoneInfo(settings.timezone)

    def require_auth(credentials: HTTPBasicCredentials | None=Depends(basic)):
        if not settings.web_auth_enabled:
            return
        valid = bool(credentials) and secrets.compare_digest(credentials.username, settings.web_username) and secrets.compare_digest(credentials.password, settings.web_password)
        if not valid:
            raise HTTPException(401, 'Authentication required', headers={'WWW-Authenticate': 'Basic'})

    def require_advisor_token(credentials: HTTPAuthorizationCredentials | None=Depends(advisor_bearer)):
        configured = settings.advisor_api_token
        if not configured:
            raise HTTPException(503, 'AI 顾问入口尚未配置')
        valid = bool(credentials) and secrets.compare_digest(credentials.credentials, configured)
        if not valid:
            raise HTTPException(401, 'Invalid advisor token', headers={'WWW-Authenticate': 'Bearer'})
    auth = [Depends(require_auth)]
    registry = registry or REGENERATION_REGISTRY

    def third_party_models():
        return [str(name) for name in getattr(ollama, 'remote_models', []) or []]

    async def available_models():
        lister = getattr(ollama, 'models', None)
        names = await lister() if lister else []
        return sorted(set(names) | {ollama.model})

    def pattern_list(status=None, limit=100, offset=0):
        params = []
        where = ''
        if status:
            if status not in PATTERN_STATUSES:
                raise HTTPException(400, 'invalid pattern status')
            where = 'WHERE status=?'
            params.append(status)
        params.extend([min(max(limit, 1), 200), max(offset, 0)])
        rows = db.rows(f'SELECT * FROM patterns {where} ORDER BY datetime(last_seen) DESC,id DESC LIMIT ? OFFSET ?', params)
        return [_pattern(row, local_tz) for row in rows]

    def memory_list(limit=200, offset=0, status=None):
        if status and status not in PATTERN_STATUSES:
            raise HTTPException(400, 'invalid memory status')
        where = 'WHERE status=?' if status else ''
        params = [status] if status else []
        params.extend([min(max(limit, 1), 200), max(offset, 0)])
        rows = db.rows(f'SELECT * FROM memories {where} ORDER BY datetime(updated_at) DESC,id DESC LIMIT ? OFFSET ?', params)
        for row in rows:
            row['created_at'] = _as_timezone(row.get('created_at'), local_tz)
            row['updated_at'] = _as_timezone(row.get('updated_at'), local_tz)
        return rows

    def chinese_label(value, seconds=True):
        return time_display.chinese_label(value, seconds=seconds, tz=local_tz)

    def moment(value):
        return time_display.moment(value, tz=local_tz)
    templates.env.filters['moment'] = moment

    def period_label(start, end):
        try:
            first = datetime.fromisoformat(str(start)).astimezone(local_tz)
            last = datetime.fromisoformat(str(end)).astimezone(local_tz) if end else None
        except (TypeError, ValueError):
            return None
        if last is None:
            return f'{first.month}月{first.day}日 {first:%H:%M}'
        if first.date() == last.date():
            return f'{first.month}月{first.day}日 {first:%H:%M}–{last:%H:%M}'
        if last.date() == first.date() + timedelta(days=1):
            return f'{first.month}月{first.day}日 {first:%H:%M}–次日 {last:%H:%M}'
        return f'{first.month}月{first.day}日 {first:%H:%M}–{last.month}月{last.day}日 {last:%H:%M}'

    def promote_report_fields(detail):
        summary = detail.get('summary')
        if not isinstance(summary, dict):
            return
        detail['generation_status'] = summary.pop('generation_status', None)
        detail['analysis_scope'] = summary.pop('analysis_scope', None)
        detail['error_type'] = summary.pop('error_type', None)
        detail['legacy_shape'] = False
        detail['reference_model'] = summary.pop('reference_model', None)
        detail['reference_period'] = summary.pop('reference_period', None)
        status_class, status_label = REPORT_STATUS_LABELS.get(detail['generation_status'], (None, None))
        detail['status_class'], detail['status_label'] = (status_class, status_label)
        if 'summary' in summary:
            return
        legacy = _legacy_summary(summary)
        if legacy is not None:
            detail['summary'] = {'summary': legacy}
            detail['legacy_shape'] = True

    def resolve_key(table, key, selected):
        for candidate in dict.fromkeys((selected, _restore_offset(selected))):
            row = db.rows(f'SELECT * FROM {table} WHERE {key}=? LIMIT 1', (candidate,))
            if row:
                return row[0]
        return None
    scope_by_table = {'period_summaries': 'period', 'daily_summaries': 'daily', 'weekly_summaries': 'weekly'}

    def variant_list(scope, report_key):
        rows = db.rows('SELECT llm_model,created_at,llm_duration_ms FROM report_variants WHERE scope=? AND report_key=? ORDER BY created_at DESC, llm_model', (scope, report_key))
        for row in rows:
            row['created_at'] = _as_timezone(row.get('created_at'), local_tz)
        return rows

    def variant_report(scope, report_key, model):
        rows = db.rows('SELECT llm_model,summary_json,created_at,llm_duration_ms FROM report_variants WHERE scope=? AND report_key=? AND llm_model=? LIMIT 1', (scope, report_key, model))
        return rows[0] if rows else None

    def summaries(table, key, selected=None, limit=60, variant=None):
        profile = getattr(event_filter, 'profile', None)
        allowed = {('daily_summaries', 'day'), ('weekly_summaries', 'week_start'), ('period_summaries', 'period_start')}
        if (table, key) not in allowed:
            raise ValueError('invalid summary source')
        extra = ',period_end' if table == 'period_summaries' else ''
        listing = db.rows(f'SELECT {key}{extra},created_at,llm_model,llm_duration_ms FROM {table} ORDER BY {key} DESC LIMIT ?', (min(max(limit, 1), 200),))
        detail = None
        if selected:
            detail = resolve_key(table, key, selected)
        elif listing:
            row = db.rows(f'SELECT * FROM {table} WHERE {key}=? LIMIT 1', (listing[0][key],))
            detail = row[0] if row else None
        scope = scope_by_table[table]
        chosen_variant = None
        if detail and variant:
            stored = variant_report(scope, detail[key], variant)
            if stored:
                chosen_variant = stored['llm_model']
                detail['summary_json'] = stored['summary_json']
                detail['created_at'] = stored['created_at']
                detail['llm_model'] = stored['llm_model']
                detail['llm_duration_ms'] = stored['llm_duration_ms']
        if detail:
            detail['summary'] = _json(detail.pop('summary_json'), {})
            detail['created_at'] = _as_timezone(detail.get('created_at'), local_tz)
            promote_report_fields(detail)
            detail['display_summary'] = readable_report(detail.get('summary'), db, profile)
            detail['display_body'] = report_body(detail['display_summary'])
            detail['display_metadata'] = {name: detail['display_summary'][name] for name in REPORT_DIAGNOSTIC_FIELDS if isinstance(detail['display_summary'], dict) and detail['display_summary'].get(name) not in (None, '', [], {})}
            detail['variants'] = variant_list(scope, detail[key])
            detail['variant_model'] = chosen_variant
            for item in detail['variants']:
                item['current'] = item['llm_model'] == chosen_variant if chosen_variant else item['llm_model'] == detail.get('llm_model')
            detail['show_variants'] = bool(detail['variants']) and (len(detail['variants']) > 1 or not any((item['current'] for item in detail['variants'])))
            detail['regenerating'] = (scope_by_table[table], detail[key]) in registry.in_flight
            detail['distilling'] = ('distill', f'{scope}:{detail[key]}') in registry.in_flight
            detail['can_distill'] = bool(analyzer is not None and scope in ('period', 'daily', 'weekly') and analyzer.reference_report(scope, analyzer.local_model(), key=detail[key]) and analyzer._own_stored_report(scope, detail[key]))
            detail['display_label'] = period_label(detail.get(key), detail.get('period_end')) if table == 'period_summaries' else chinese_label(detail.get(key))
            detail['created_label'] = chinese_label(detail.get('created_at'))
        for item in listing:
            item['created_at'] = _as_timezone(item.get('created_at'), local_tz)
            item['display_label'] = period_label(item.get(key), item.get('period_end')) if table == 'period_summaries' else chinese_label(item.get(key))
            item['label'] = item['display_label']
        return (listing, detail)

    def event_list(domain=None, entity=None, event_type=None, from_ts=None, to_ts=None, limit=50, offset=0):
        clauses, params = ([], [])
        for column, value, operator in (('domain', domain, '='), ('entity_id', entity, 'LIKE'), ('event_type', event_type, '='), ('ts', from_ts, '>='), ('ts', to_ts, '<=')):
            if value:
                if column == 'ts':
                    clauses.append(f'datetime({column}) {operator} datetime(?)')
                    try:
                        parsed = datetime.fromisoformat(value)
                    except ValueError as error:
                        raise HTTPException(400, 'invalid time filter') from error
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=local_tz)
                    params.append(parsed.astimezone(timezone.utc).isoformat())
                else:
                    clauses.append(f'{column} {operator} ?')
                    params.append(f'%{value}%' if operator == 'LIKE' else value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
        page_size = min(max(limit, 1), 200)
        params.extend([page_size, max(offset, 0)])
        rows = db.rows(f'SELECT id,ts,domain,entity_id,category,event_type,source,old_state,new_state,area,attributes_json,metadata_json FROM events {where} ORDER BY id DESC LIMIT ? OFFSET ?', params)
        for row in rows:
            row['attributes'] = _json(row.pop('attributes_json'), {})
            row['metadata'] = _json(row.pop('metadata_json'), {})
            row['ts'] = _as_timezone(row.get('ts'), local_tz)
        return rows

    def session_room_display(room):
        profile = getattr(event_filter, 'profile', None)
        if profile and looks_like_entity_id(room):
            entry = profile.entities.get(str(room))
            if entry:
                return entry.get('area') or entry.get('device') or room
        return room

    def session_list(limit=50, offset=0):
        rows = db.rows('SELECT * FROM sessions ORDER BY datetime(start_ts) DESC,id DESC LIMIT ? OFFSET ?', (min(max(limit, 1), 200), max(offset, 0)))
        for row in rows:
            row['evidence'] = _json(row.pop('evidence_json'), [])
            row['start_ts'] = _as_timezone(row.get('start_ts'), local_tz)
            row['end_ts'] = _as_timezone(row.get('end_ts'), local_tz)
            row['room_display'] = session_room_display(row.get('room'))
        return rows

    def sessions_by_room(per_room=3):
        rows = db.rows('SELECT * FROM (  SELECT *, ROW_NUMBER() OVER (    PARTITION BY room ORDER BY datetime(start_ts) DESC, id DESC  ) AS rn FROM sessions) WHERE rn <= ? ORDER BY datetime(start_ts) DESC, id DESC', (int(per_room),))
        grouped = {}
        for row in rows:
            row = dict(row)
            row.pop('rn', None)
            row['evidence'] = _json(row.pop('evidence_json'), [])
            row['start_ts'] = _as_timezone(row.get('start_ts'), local_tz)
            row['end_ts'] = _as_timezone(row.get('end_ts'), local_tz)
            row['room_display'] = session_room_display(row.get('room'))
            room = row.get('room_display') or row.get('room') or '未知房间'
            if looks_like_entity_id(room):
                continue
            grouped.setdefault(room, []).append(row)
        blocks = []
        for room, items in grouped.items():
            items.sort(key=lambda r: r.get('start_ts') or '', reverse=True)
            blocks.append({'room': room, 'sessions': items[:per_room]})
        blocks.sort(key=lambda b: (b['sessions'][0]['start_ts'] if b['sessions'] else '', b['room']), reverse=True)
        return blocks

    def suggestion_list(limit=100, offset=0):
        rows = db.rows('SELECT * FROM automation_suggestions ORDER BY datetime(created_at) DESC,id DESC LIMIT ? OFFSET ?', (min(max(limit, 1), 200), max(offset, 0)))
        for row in rows:
            row['evidence'] = _json(row.pop('evidence_json'), [])
            row['confidence_percent'] = round(float(row.get('confidence') or 0) * 100)
            row['created_at'] = _as_timezone(row.get('created_at'), local_tz)
        return rows

    def guidance_list(active_only=False, limit=100):
        where = 'WHERE active=1' if active_only else ''
        rows = db.rows(f'SELECT id,content,scope,source,active,created_at,archived_at FROM ai_guidance {where} ORDER BY id DESC LIMIT ?', (min(max(limit, 1), 200),))
        for row in rows:
            row['active'] = bool(row['active'])
            row['created_at'] = _as_timezone(row.get('created_at'), local_tz)
            row['archived_at'] = _as_timezone(row.get('archived_at'), local_tz)
        return rows

    def advisor_list(status=None, limit=100):
        if status and status not in {'pending', 'approved', 'rejected'}:
            raise HTTPException(400, 'invalid advisor status')
        where, params = ('WHERE status=?', [status]) if status else ('', [])
        params.append(min(max(limit, 1), 200))
        rows = db.rows(f'SELECT id,external_id,source,scope,content,status,created_at,reviewed_at,guidance_id FROM advisor_submissions {where} ORDER BY id DESC LIMIT ?', params)
        for row in rows:
            row['created_at'] = _as_timezone(row.get('created_at'), local_tz)
            row['reviewed_at'] = _as_timezone(row.get('reviewed_at'), local_tz)
        return rows

    def maintainer_board():
        raw = db.scalar("SELECT value FROM system_state WHERE key='ai_maintainers'")
        data = _json(raw, {})
        if not isinstance(data, dict):
            data = {}
        scores_raw = db.scalar("SELECT value FROM system_state WHERE key='ai_maintainer_scores'")
        scores_map = _json(scores_raw, {})
        if not isinstance(scores_map, dict):
            scores_map = {}
        rows = []

        def detail_rows(target_scores):
            built = []
            if isinstance(target_scores, dict):
                for scorer, item in target_scores.items():
                    if not isinstance(item, dict):
                        continue
                    try:
                        value = int(item.get('score'))
                    except (TypeError, ValueError):
                        continue
                    built.append({'scorer': str(scorer)[:80], 'score': max(0, min(100, value)), 'note': str(item.get('note') or '')[:200], 'at': item.get('at'), 'at_label': chinese_label(item.get('at'))})
            built.sort(key=lambda item: (-item['score'], item['scorer']))
            return built
        for name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            last_at = entry.get('last_at')
            target_scores = scores_map.get(name)
            score_rows = detail_rows(target_scores)
            score_rows.sort(key=lambda item: (-item['score'], item['scorer']))
            values = _scored_values(target_scores)
            average = round(sum(values) / len(values), 1) if values else None
            rows.append({'name': str(name)[:80], 'count': int(entry.get('count') or 0), 'last_at': last_at, 'last_at_label': chinese_label(last_at), 'last_note': str(entry.get('last_note') or '')[:200], 'score_avg': average, 'score_count': len(score_rows), 'scores': score_rows, 'unregistered': False})
        for name in scores_map:
            if name in data:
                continue
            score_rows = detail_rows(scores_map.get(name))
            if not score_rows:
                continue
            rows.append({'name': str(name)[:80], 'count': 0, 'last_at': None, 'last_at_label': None, 'last_note': '', 'score_avg': round(sum((row['score'] for row in score_rows)) / len(score_rows), 1), 'score_count': len(score_rows), 'scores': score_rows, 'unregistered': True})
        rows.sort(key=lambda item: (-item['count'], -(item['score_avg'] or 0), item['name']))
        return rows

    def record_maintenance(name, note=None):
        name = (name or '').strip()
        if not name:
            raise HTTPException(400, 'name is required')
        data = _json(db.scalar("SELECT value FROM system_state WHERE key='ai_maintainers'") or '{}', {})
        if not isinstance(data, dict):
            data = {}
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {'count': 0}
        entry['count'] = int(entry.get('count') or 0) + 1
        entry['last_at'] = datetime.now(timezone.utc).isoformat()
        cleaned = (note or '').strip()
        if cleaned:
            entry['last_note'] = cleaned[:200]
        data[name] = entry
        db.set_state('ai_maintainers', json.dumps(data, ensure_ascii=False))
        logger.info('maintenance recorded name=%s count=%s', name[:40], entry['count'])
        return {'name': name, 'count': entry['count'], 'last_note': entry.get('last_note'), 'day_increment': True}

    def record_maintainer_score(target, scorer, score, note=None):
        target = (target or '').strip()
        scorer = (scorer or '').strip()
        if not target or not scorer:
            raise HTTPException(400, 'target and scorer are required')
        if target == scorer:
            raise HTTPException(400, '不能给自己打分')
        score = int(score)
        if score < 0 or score > 100:
            raise HTTPException(400, 'score must be 0–100')
        data = _json(db.scalar("SELECT value FROM system_state WHERE key='ai_maintainer_scores'") or '{}', {})
        if not isinstance(data, dict):
            data = {}
        target_scores = data.get(target)
        if not isinstance(target_scores, dict):
            target_scores = {}
        cleaned = (note or '').strip()
        previous = target_scores.get(scorer)
        entry = {'score': score, 'at': datetime.now(timezone.utc).isoformat()}
        if cleaned:
            entry['note'] = cleaned[:200]
        elif isinstance(previous, dict) and str(previous.get('note') or '').strip():
            entry['note'] = str(previous['note']).strip()[:200]
        target_scores[scorer] = entry
        data[target] = target_scores
        db.set_state('ai_maintainer_scores', json.dumps(data, ensure_ascii=False))
        values = _scored_values(target_scores)
        average = round(sum(values) / len(values), 1) if values else None
        logger.info('maintainer score target=%s scorer=%s score=%s avg=%s', target[:40], scorer[:40], score, average)
        return {'target': target, 'scorer': scorer, 'score': score, 'score_avg': average, 'score_count': len(values)}

    async def status_data():
        states = {r['key']: r['value'] for r in db.rows('SELECT key,value FROM system_state')}
        collection_start = states.get('collection_started_at')
        events = db.scalar('SELECT COUNT(*) FROM events WHERE datetime(ts)>=datetime(?)', (collection_start,)) if collection_start else db.scalar('SELECT COUNT(*) FROM events')
        now = time.monotonic()
        if now - float(_database_ok_cache['at'] or 0) >= _HEALTH_CACHE_SECONDS:
            try:
                _database_ok_cache['value'] = db.scalar('PRAGMA quick_check') == 'ok'
            except Exception:
                _database_ok_cache['value'] = False
            _database_ok_cache['at'] = now
        database_ok = bool(_database_ok_cache['value'])
        if now - float(_ollama_ok_cache['at'] or 0) >= _HEALTH_CACHE_SECONDS:
            _ollama_ok_cache['value'] = await ollama.healthy()
            _ollama_ok_cache['at'] = now
        ollama_ok = bool(_ollama_ok_cache['value'])
        stored_cooccurrence = _json(states.get('cooccurrence_status'), {})
        if not isinstance(stored_cooccurrence, dict):
            stored_cooccurrence = {}
        if cooccurrence is not None:
            stored_cooccurrence['enabled'] = True
            stored_cooccurrence['algorithm_version'] = 'cooccurrence-v1'
            stored_cooccurrence['runtime_config_hash'] = cooccurrence.config.semantic_hash
        else:
            stored_cooccurrence = {'enabled': False}
        profile = getattr(event_filter, 'profile', None)
        configured_collect_count = sum((1 for item in profile.entities.values() if item['collect'])) if profile else len(event_filter.include)
        newest_period = db.rows('SELECT summary_json FROM period_summaries ORDER BY period_start DESC LIMIT 1')
        last_period_status = None
        if newest_period:
            stored_period = _json(newest_period[0].get('summary_json'), {})
            if isinstance(stored_period, dict):
                last_period_status = stored_period.get('generation_status')
        period_job = _json(states.get('period_job_status'), {})
        if not isinstance(period_job, dict):
            period_job = {}
        pending_periods = [start.isoformat() for start, _ in analyzer.pending_period_bounds()] if analyzer else []
        last_period = db.scalar('SELECT MAX(period_end) FROM period_summaries')
        last_daily = db.scalar('SELECT MAX(day) FROM daily_summaries')
        last_weekly = db.scalar('SELECT MAX(week_start) FROM weekly_summaries')
        last_event = _as_timezone(states.get('last_ha_event'), local_tz)
        collection_started = _as_timezone(collection_start, local_tz)
        first_event = db.scalar('SELECT MIN(ts) FROM events')
        learning_days = int(getattr(settings, 'learning_days', 7))
        min_pattern_samples = int(getattr(settings, 'min_pattern_samples', 5))
        learning_ready_at = None
        learning_mature = False
        if first_event:
            try:
                first_event_at = datetime.fromisoformat(str(first_event).replace('Z', '+00:00'))
                learning_ready_at = (first_event_at + timedelta(days=learning_days)).astimezone(local_tz).isoformat()
                learning_mature = datetime.now(timezone.utc) >= first_event_at.astimezone(timezone.utc) + timedelta(days=learning_days)
            except (TypeError, ValueError):
                logger.warning('invalid first event timestamp while calculating learning readiness')
        if 'lookback_end' in stored_cooccurrence:
            stored_cooccurrence['lookback_end_label'] = chinese_label(stored_cooccurrence.get('lookback_end'))
        remote = getattr(ollama, 'remote', None)
        third_party_status = {'enabled': remote is not None, 'models': third_party_models(), 'default_model': settings.third_party_default_model or None}
        return {'home_assistant': {'connected': ha.connected, 'last_event': last_event, 'last_event_label': chinese_label(last_event), 'last_event_relative': _relative(states.get('last_ha_event'))}, 'ollama': {'connected': ollama_ok, 'model': ollama.model}, 'third_party': third_party_status, 'database': {'ok': database_ok, 'path': str(db.path), 'size_bytes': db.path.stat().st_size if db.path.exists() else 0}, 'maintainers': maintainer_board(), 'analyzer': {'last_period': last_period, 'last_period_label': chinese_label(last_period), 'last_period_status': last_period_status, 'last_daily': last_daily, 'last_daily_label': chinese_label(last_daily), 'last_weekly': last_weekly, 'last_weekly_label': chinese_label(last_weekly), 'learning_only': settings.learning_only, 'learning_days': learning_days, 'min_pattern_samples': min_pattern_samples, 'learning_mature': learning_mature, 'learning_ready_at': learning_ready_at, 'learning_ready_label': chinese_label(learning_ready_at), 'prompt_sha256': states.get('system_prompt_sha256'), 'period_job': period_job, 'pending_periods': pending_periods, 'regeneration': regeneration_queue(), 'distill_job': _json(states.get('distill_job_status'), {}), 'question_job': _json(states.get('question_job_status'), {})}, 'counts': {'events': events, 'total_events': db.scalar('SELECT COUNT(*) FROM events'), 'sessions': db.scalar('SELECT COUNT(*) FROM sessions'), 'candidate_patterns': db.scalar("SELECT COUNT(*) FROM patterns WHERE status='candidate'"), 'confirmed_patterns': db.scalar("SELECT COUNT(*) FROM patterns WHERE status='confirmed'"), 'rejected_patterns': db.scalar("SELECT COUNT(*) FROM patterns WHERE status='rejected'"), 'memories': db.scalar("SELECT COUNT(*) FROM memories WHERE status='confirmed'"), 'suggestions': db.scalar('SELECT COUNT(*) FROM automation_suggestions')}, 'collector': {'collection_started_at': collection_started, 'collection_started_label': chinese_label(collection_started), 'monitored_domains': sorted(event_filter.domains), 'monitored_entity_count': int(states.get('monitored_entity_count', 0)), 'explicit_entity_count': configured_collect_count, 'entity_profile_enabled': profile is not None, 'entity_profile_id': profile.profile_id if profile else None, 'entity_profile_entry_count': len(profile.entities) if profile else 0, 'entity_coverage': _json(states.get('entity_coverage'), {})}, 'cooccurrence': stored_cooccurrence, 'segments': analyzer.segment_memory.buffer_state() if analyzer is not None and analyzer.segment_memory else None}

    def apply_feedback(pattern_id, action, note=None):
        if action not in {'confirm', 'reject'}:
            raise HTTPException(400, 'action must be confirm or reject')
        current = db.rows('SELECT status FROM patterns WHERE id=?', (pattern_id,))
        if not current:
            raise HTTPException(404, 'pattern not found')
        if current[0]['status'] != 'candidate':
            raise HTTPException(409, 'only candidate patterns can receive feedback')
        if not memory.feedback(pattern_id, action, note):
            raise HTTPException(404, 'pattern not found')
        return {'ok': True, 'pattern_id': pattern_id, 'status': 'confirmed' if action == 'confirm' else 'rejected'}

    @router.get('/api/dashboard', dependencies=auth)
    async def api_dashboard():
        status = await status_data()
        _, daily = summaries('daily_summaries', 'day', limit=1)
        _, period = summaries('period_summaries', 'period_start', limit=1)
        return {'status': status, 'recent_patterns': pattern_list(limit=10), 'latest_period': period, 'latest_daily': daily, 'active_guidance': guidance_list(active_only=True, limit=20)}

    @router.get('/api/guidance', dependencies=auth)
    def api_guidance(active_only: bool=False, limit: int=Query(100, ge=1, le=200)):
        return {'items': guidance_list(active_only, limit)}

    @router.post('/api/advisor/inbox', dependencies=[Depends(require_advisor_token)], status_code=202)
    def api_advisor_inbox(body: AdvisorSubmissionBody):
        content = body.content.strip()
        if not content:
            raise HTTPException(422, '建议不能为空')
        existing = db.rows('SELECT id,status FROM advisor_submissions WHERE external_id=?', (body.external_id,))
        if existing:
            return {'accepted': True, 'duplicate': True, 'id': existing[0]['id'], 'status': existing[0]['status']}
        now = datetime.now(timezone.utc).isoformat()
        with db.connect() as con:
            cursor = con.execute('INSERT INTO advisor_submissions(external_id,source,scope,content,created_at) VALUES(?,?,?,?,?)', (body.external_id, body.source.strip(), body.scope, content, now))
        return {'accepted': True, 'duplicate': False, 'id': cursor.lastrowid, 'status': 'pending'}

    @router.get('/api/condense/input', dependencies=[Depends(require_advisor_token)])
    def api_condense_input(limit: int=Query(200, ge=1, le=500)):
        return condense.input_bundle(db, limit=limit)

    @router.post('/api/condense/proposal', dependencies=[Depends(require_advisor_token)], status_code=202)
    def api_condense_proposal(body: CondenseProposalBody):
        try:
            return condense.submit(db, body.source, body.external_id, [rule.model_dump() for rule in body.rules], body.notes)
        except condense.CondenseValidationError as error:
            raise HTTPException(422, str(error)) from error

    @router.get('/api/condense/proposals', dependencies=auth)
    def api_condense_proposals(status: str | None=None, limit: int=Query(50, ge=1, le=200)):
        return {'items': condense.proposals(db, status, limit)}

    @router.post('/api/condense/proposals/{proposal_id}/approve', dependencies=auth)
    def api_condense_approve(proposal_id: int):
        try:
            return condense.approve(db, proposal_id)
        except condense.CondenseValidationError as error:
            raise HTTPException(409, str(error)) from error

    @router.post('/api/condense/proposals/{proposal_id}/reject', dependencies=auth)
    def api_condense_reject(proposal_id: int):
        try:
            return condense.reject(db, proposal_id)
        except condense.CondenseValidationError as error:
            raise HTTPException(409, str(error)) from error

    @router.get('/api/advisor/submissions', dependencies=auth)
    def api_advisor_submissions(status: str | None=None, limit: int=Query(100, ge=1, le=200)):
        return {'items': advisor_list(status, limit)}

    @router.post('/api/advisor/submissions/{submission_id}/approve', dependencies=auth)
    def api_approve_advisor_submission(submission_id: int):
        now = datetime.now(timezone.utc).isoformat()
        with db.connect() as con:
            row = con.execute('SELECT source,scope,content,status FROM advisor_submissions WHERE id=?', (submission_id,)).fetchone()
            if not row:
                raise HTTPException(404, '未找到顾问建议')
            if row['status'] != 'pending':
                raise HTTPException(409, '该建议已经审核')
            cursor = con.execute('INSERT INTO ai_guidance(content,scope,source,created_at) VALUES(?,?,?,?)', (row['content'], row['scope'], row['source'], now))
            con.execute("UPDATE advisor_submissions SET status='approved',reviewed_at=?,guidance_id=? WHERE id=?", (now, cursor.lastrowid, submission_id))
        return {'ok': True, 'id': submission_id, 'status': 'approved', 'guidance_id': cursor.lastrowid}

    @router.post('/api/advisor/submissions/{submission_id}/reject', dependencies=auth)
    def api_reject_advisor_submission(submission_id: int):
        now = datetime.now(timezone.utc).isoformat()
        with db.connect() as con:
            cursor = con.execute("UPDATE advisor_submissions SET status='rejected',reviewed_at=? WHERE id=? AND status='pending'", (now, submission_id))
        if cursor.rowcount != 1:
            raise HTTPException(404, '未找到待审核建议')
        return {'ok': True, 'id': submission_id, 'status': 'rejected'}

    @router.post('/api/guidance', dependencies=auth, status_code=201)
    def api_add_guidance(body: GuidanceBody):
        content = body.content.strip()
        if not content:
            raise HTTPException(422, '建议不能为空')
        now = datetime.now(timezone.utc).isoformat()
        with db.connect() as con:
            cursor = con.execute('INSERT INTO ai_guidance(content,scope,source,created_at) VALUES(?,?,?,?)', (content, body.scope, 'owner', now))
        return {'ok': True, 'id': cursor.lastrowid, 'content': content, 'scope': body.scope, 'source': 'owner', 'created_at': _as_timezone(now, local_tz), 'active': True}

    @router.post('/api/guidance/{guidance_id}/archive', dependencies=auth)
    def api_archive_guidance(guidance_id: int):
        now = datetime.now(timezone.utc).isoformat()
        with db.connect() as con:
            cursor = con.execute('UPDATE ai_guidance SET active=0,archived_at=? WHERE id=? AND active=1', (now, guidance_id))
        if cursor.rowcount != 1:
            raise HTTPException(404, '未找到有效建议')
        return {'ok': True, 'id': guidance_id, 'active': False}

    @router.get('/api/patterns', dependencies=auth)
    def api_patterns(status: str | None=None, limit: int=Query(100, ge=1, le=200), offset: int=Query(0, ge=0)):
        return {'items': pattern_list(status, limit, offset), 'limit': limit, 'offset': offset}

    @router.get('/api/patterns/{pattern_id}', dependencies=auth)
    def api_pattern(pattern_id: int):
        rows = db.rows('SELECT * FROM patterns WHERE id=?', (pattern_id,))
        if not rows:
            raise HTTPException(404, 'pattern not found')
        item = _pattern(rows[0], local_tz)
        item['feedback'] = db.rows('SELECT action,note,created_at FROM feedback WHERE pattern_id=? ORDER BY id', (pattern_id,))
        for feedback in item['feedback']:
            feedback['created_at'] = _as_timezone(feedback.get('created_at'), local_tz)
        return item

    @router.post('/api/patterns/{pattern_id}/confirm', dependencies=auth)
    def api_confirm(pattern_id: int, body: FeedbackBody | None=None):
        return apply_feedback(pattern_id, 'confirm', body.note if body else None)

    @router.post('/api/patterns/{pattern_id}/reject', dependencies=auth)
    def api_reject(pattern_id: int, body: FeedbackBody | None=None):
        return apply_feedback(pattern_id, 'reject', body.note if body else None)

    @router.post('/api/patterns/{pattern_id}/feedback', dependencies=auth)
    def api_feedback_legacy(pattern_id: int, body: LegacyFeedbackBody):
        return apply_feedback(pattern_id, body.action, body.note)

    @router.get('/api/memories', dependencies=auth)
    def api_memories(status: str | None=None, limit: int=Query(100, ge=1, le=200), offset: int=Query(0, ge=0)):
        return {'items': memory_list(limit, offset, status), 'limit': limit, 'offset': offset}

    @router.get('/api/daily', dependencies=auth)
    def api_daily(day: str | None=None, model: str | None=None, limit: int=Query(60, ge=1, le=200)):
        listing, detail = summaries('daily_summaries', 'day', day, limit, model)
        return {'items': listing, 'selected': detail}

    @router.get('/api/periods', dependencies=auth)
    def api_periods(period_start: str | None=None, model: str | None=None, limit: int=Query(60, ge=1, le=200)):
        listing, detail = summaries('period_summaries', 'period_start', period_start, limit, model)
        return {'items': listing, 'selected': detail}

    @router.post('/api/reports/regenerate', dependencies=auth, status_code=202)
    async def api_regenerate_report(body: RegenerateBody):
        if analyzer is None:
            raise HTTPException(503, '分析器未配置')
        table, key_column = REGENERATION_SOURCES[body.scope]
        candidate = _restore_offset(body.key.strip())
        columns = 'period_end,summary_json' if body.scope == 'period' else 'summary_json'
        row = db.rows(f'SELECT {columns} FROM {table} WHERE {key_column}=? LIMIT 1', (candidate,))
        if not row:
            raise HTTPException(404, '未找到该报告')
        status = None
        stored = _json(row[0].get('summary_json'), {})
        if isinstance(stored, dict):
            status = stored.get('generation_status')
        run_value = row[0]['period_end'] if body.scope == 'period' else candidate
        if status in PERIOD_NON_TERMINAL:
            raise HTTPException(409, '该报告正在生成中，请等它结束后再试')
        if body.model and body.model not in await available_models():
            raise HTTPException(422, '该模型当前不可用')
        if not registry.add(body.scope, candidate, run_value, body.model):
            raise HTTPException(409, '该报告正在重新生成中，请稍后再试')
        logger.info('manual report regeneration requested scope=%s', body.scope)
        return {'ok': True, 'scope': body.scope, 'key': candidate, 'model': body.model or ollama.model, 'status': 'started'}

    @router.post('/api/reports/distill', dependencies=auth, status_code=202)
    async def api_distill_report(body: DistillBody):
        if analyzer is None:
            raise HTTPException(503, '分析器未配置')
        key = _restore_offset(body.key.strip())
        table, column = REGENERATION_SOURCES[body.scope]
        if not db.rows(f'SELECT {column} FROM {table} WHERE {column}=? LIMIT 1', (key,)):
            raise HTTPException(404, '未找到该报告')
        teacher = analyzer.reference_report(body.scope, analyzer.local_model(), key=key)
        if not teacher:
            raise HTTPException(409, '该报告还没有其他模型的版本可以学习')
        if not analyzer._own_stored_report(body.scope, key):
            raise HTTPException(409, '本地模型还没有它自己写的这份报告，无从对比学习')
        queue_key = f'{body.scope}:{key}'
        if not registry.add('distill', queue_key, queue_key, 'local'):
            raise HTTPException(409, '该报告正在蒸馏中，请稍后再试')
        logger.info('manual distillation requested scope=%s', body.scope)
        return {'ok': True, 'scope': body.scope, 'key': key, 'teacher': teacher.get('model'), 'status': 'started'}

    @router.get('/api/weekly', dependencies=auth)
    def api_weekly(week_start: str | None=None, model: str | None=None, limit: int=Query(60, ge=1, le=200)):
        listing, detail = summaries('weekly_summaries', 'week_start', week_start, limit, model)
        return {'items': listing, 'selected': detail}

    @router.get('/api/suggestions', dependencies=auth)
    def api_suggestions(limit: int=Query(100, ge=1, le=200), offset: int=Query(0, ge=0)):
        return {'items': suggestion_list(limit, offset), 'limit': limit, 'offset': offset}

    @router.get('/api/events', dependencies=auth)
    def api_events(domain: str | None=None, entity: str | None=None, event_type: str | None=None, from_ts: str | None=None, to_ts: str | None=None, limit: int=Query(50, ge=1, le=200), offset: int=Query(0, ge=0)):
        return {'items': event_list(domain, entity, event_type, from_ts, to_ts, limit, offset), 'limit': limit, 'offset': offset}

    @router.get('/api/sessions', dependencies=auth)
    def api_sessions(limit: int=Query(50, ge=1, le=200), offset: int=Query(0, ge=0)):
        return {'items': session_list(limit, offset), 'limit': limit, 'offset': offset}

    @router.get('/api/segments', dependencies=auth)
    def api_segments(limit: int=Query(20, ge=1, le=100)):
        rows = db.rows('SELECT id,period_start,period_end,start_event_id,end_event_id,event_count,estimated_input_tokens,status,retry_count,last_error,model,summary_level,summary_text,created_at,updated_at FROM event_segments ORDER BY id DESC LIMIT ?', (min(max(limit, 1), 100),))
        for row in rows:
            row['period_start'] = _as_timezone(row['period_start'], local_tz)
            row['period_end'] = _as_timezone(row['period_end'], local_tz)
            row['created_at'] = _as_timezone(row['created_at'], local_tz)
            row['updated_at'] = _as_timezone(row['updated_at'], local_tz)
        buffer_state = analyzer.segment_memory.buffer_state() if analyzer is not None and analyzer.segment_memory else None
        return {'items': rows, 'buffer': buffer_state}

    @router.get('/api/segments/{segment_id}', dependencies=auth)
    def api_segment_detail(segment_id: int):
        rows = db.rows('SELECT * FROM event_segments WHERE id=?', (segment_id,))
        if not rows:
            raise HTTPException(404, '未找到该记忆段')
        return {'item': rows[0]}

    def describe_job(job):
        if not job:
            return None
        if job['scope'] == 'period':
            end = datetime.fromisoformat(str(job['value']))
            label = period_label(end - timedelta(hours=6), end)
        elif job['scope'] == 'distill':
            report_scope, _, report_key = str(job['value']).partition(':')
            if report_scope == 'period':
                start = datetime.fromisoformat(report_key)
                label = f'{period_label(start, start + timedelta(hours=6))} · 蒸馏'
            elif report_scope == 'weekly':
                label = f'{chinese_label(report_key)} · 周报蒸馏'
            elif report_scope == 'daily':
                label = f'{chinese_label(report_key)} · 日报蒸馏'
            else:
                label = str(job['value'])
        elif job['scope'] == 'questions':
            label = '让 AI 提问'
        elif job['scope'] == 'segment':
            label = f"分层记忆段 · {job.get('label') or job['key']}"
        elif job['scope'] == 'daily':
            label = f"日报 · {chinese_label(job['value'])}"
        elif job['scope'] == 'weekly':
            label = f"周报 · {chinese_label(job['value'])}"
        else:
            label = chinese_label(job['value']) or str(job.get('label') or job['key'])
        return {'scope': job['scope'], 'key': job['key'], 'model': job.get('model') or getattr(ollama, 'model', None), 'label': label or job['key'], 'queued_label': chinese_label(job.get('queued_at')), 'source': job.get('source')}

    def _live_system_job(state, scope, label_prefix=''):
        if not isinstance(state, dict) or state.get('status') != 'generating':
            return None
        key = state.get('period_start') or state.get('report_key') or state.get('day') or state.get('week_start') or scope
        label = label_prefix
        if scope == 'period':
            try:
                start = datetime.fromisoformat(str(state.get('period_start')))
                end = datetime.fromisoformat(str(state.get('period_end') or start + timedelta(hours=6)))
                label = f'{period_label(start, end)} · 定时' if state.get('source') != 'manual' else period_label(start, end)
            except (TypeError, ValueError):
                label = f"六小时总结 · {chinese_label(state.get('period_start'))}"
        elif scope == 'daily':
            label = f"日报 · {chinese_label(state.get('day') or state.get('period_start'))}"
        elif scope == 'weekly':
            label = f"周报 · {chinese_label(state.get('week_start') or state.get('period_start'))}"
        elif scope == 'distill':
            label = f"蒸馏 · {chinese_label(state.get('report_key') or state.get('period_start'))}"
            if state.get('teacher_model'):
                label += f"（学 {state.get('teacher_model')}）"
        elif scope == 'questions':
            label = 'AI 提问'
        elif scope == 'segment':
            label = state.get('label') or f"分层记忆段 #{state.get('segment_id')}"
        return {'scope': scope, 'key': str(key), 'model': state.get('local_model') or state.get('model'), 'label': label, 'queued_label': chinese_label(state.get('started_at')), 'source': state.get('source') or 'scheduled'}

    def regeneration_queue():
        snapshot = registry.snapshot()
        manual_running = describe_job(snapshot['running'])
        waiting = [describe_job(job) for job in snapshot['waiting']]
        states = {r['key']: r['value'] for r in db.rows('SELECT key,value FROM system_state')}
        live = []
        for key, scope in (('period_job_status', 'period'), ('daily_job_status', 'daily'), ('weekly_job_status', 'weekly'), ('distill_job_status', 'distill'), ('question_job_status', 'questions'), ('segment_job_status', 'segment')):
            item = _live_system_job(_json(states.get(key), {}), scope)
            if item and (not (manual_running and item['scope'] == manual_running['scope'] and (str(item['key']) == str(manual_running.get('key'))))):
                live.append(item)
        if manual_running:
            live.insert(0, manual_running)
        live_period_keys = {str(it.get('key')) for it in live if it.get('scope') == 'period'}
        for start, end in analyzer.pending_period_bounds() if analyzer else []:
            label = period_label(start, end)
            start_key, end_key = (start.isoformat(), end.isoformat())
            if start_key in live_period_keys or end_key in live_period_keys:
                continue
            if any((str(w.get('key')) in (start_key, end_key) or label == w.get('label') for w in waiting if w)):
                continue
            waiting.append({'scope': 'period', 'key': start_key, 'model': None, 'label': f'{label} · 待补算', 'queued_label': None, 'source': 'catchup'})
        if analyzer is not None and analyzer.segment_memory is not None:
            seg = analyzer.segment_memory.buffer_state() or {}
            pending = int((seg.get('segments') or {}).get('pending') or 0)
            if pending:
                waiting.append({'scope': 'segment', 'key': f'pending-{pending}', 'model': None, 'label': f'分层记忆段 ×{pending} 待 AI 摘要', 'queued_label': None, 'source': 'segment'})
        rank = {'period': 0, 'daily': 0, 'weekly': 0, 'questions': 1, 'distill': 1, 'segment': 2}
        live.sort(key=lambda it: (rank.get(it.get('scope'), 3), it.get('queued_label') or ''))
        return {'running': live[0] if live else None, 'running_all': live, 'waiting': waiting}

    def provider_status():
        return provider_settings.read() if provider_settings is not None else None

    @router.get('/settings', response_class=HTMLResponse, dependencies=auth)
    async def settings_page(request: Request, status: str | None=None):
        flashes = {'saved': '已保存。', 'saved_ok': '已保存；连接测试：成功。', 'saved_fail': '已保存；连接测试失败——请检查接口地址与密钥是否正确。', 'key_cleared': '已清除保存的密钥。', 'saved_model_unavailable': '已保存；所选自动模型当前不可用，已改回本地模型。'}
        config = provider_status() or {'enabled': False, 'base_url': '', 'models': [], 'default_model': '', 'key_present': False}
        context = {'config': config, 'model_options': await available_models(), 'third_party_models': third_party_models(), 'automatic_model': config['default_model'] or ollama.model, 'flash': flashes.get(status or ''), 'active': 'settings'}
        return templates.TemplateResponse(request=request, name='settings.html', context=context)

    async def form_fields(request):
        content_type = (request.headers.get('content-type') or '').split(';')[0].strip()
        if content_type != 'application/x-www-form-urlencoded':
            return {}
        body = (await request.body()).decode('utf-8', 'replace')
        return {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}

    @router.post('/settings', response_class=HTMLResponse, dependencies=auth)
    async def save_settings(request: Request):
        if provider_settings is None:
            raise HTTPException(503, '设置存储不可用')
        fields = await form_fields(request)
        enabled = fields.get('enabled', 'false')
        base_url = fields.get('base_url', '')
        api_key = fields.get('api_key', '')
        models = fields.get('models', '')
        default_model = fields.get('default_model', '')
        clear_key = fields.get('clear_key', '')
        automatic = fields.get('automatic_model', '') or fields.get('default_model', '')
        automatic_reset = bool(automatic) and automatic not in await available_models()
        if automatic_reset:
            automatic = ''
        key_update = '' if clear_key else api_key.strip() or None
        try:
            provider_settings.save(enabled == 'true', base_url, models.split(','), automatic, key_update)
        except OSError as error:
            logger.error('provider settings write failed error=%s', type(error).__name__)
            raise HTTPException(500, '无法保存密钥文件，请检查数据目录权限') from error
        client = provider_settings.client()
        configure = getattr(ollama, 'configure', None)
        if configure is not None:
            configure(client, provider_settings.automatic_model())
        if clear_key:
            return RedirectResponse('/settings?status=key_cleared', status_code=303)
        if automatic_reset:
            return RedirectResponse('/settings?status=saved_model_unavailable', status_code=303)
        if client is None:
            return RedirectResponse('/settings?status=saved', status_code=303)
        reachable = await client.healthy()
        return RedirectResponse(f"/settings?status={('saved_ok' if reachable else 'saved_fail')}", status_code=303)

    @router.get('/api/models', dependencies=auth)
    async def api_models():
        return {'items': await available_models(), 'default': ollama.model, 'third_party': third_party_models()}

    @router.get('/api/status', dependencies=auth)
    async def api_status():
        return await status_data()

    @router.get('/', response_class=HTMLResponse, dependencies=auth)
    async def dashboard_page(request: Request):
        status = await status_data()
        _, daily = summaries('daily_summaries', 'day', limit=1)
        _, period = summaries('period_summaries', 'period_start', limit=1)
        return templates.TemplateResponse(request=request, name='dashboard.html', context={'status': status, 'patterns': pattern_list(limit=3), 'period': period, 'period_body': report_body((period or {}).get('display_summary') or (period or {}).get('summary')), 'daily': daily, 'report_labels': REPORT_FIELD_LABELS, 'guidance': guidance_list(active_only=True, limit=3), 'active': 'dashboard'})

    @router.get('/guidance', response_class=HTMLResponse, dependencies=auth)
    def guidance_page(request: Request, archived: str | None=None):
        show_archived = (archived or '').strip().lower() in ('1', 'all', 'true')
        archived_count = int(db.scalar('SELECT COUNT(*) FROM ai_guidance WHERE active=0') or 0)
        return templates.TemplateResponse(request=request, name='guidance.html', context={'guidance': guidance_list(active_only=not show_archived, limit=200 if show_archived else 100), 'archived_count': archived_count, 'showing_archived': show_archived, 'condense_proposals': condense.proposals(db, 'pending'), 'advisor_submissions': advisor_list(), 'active': 'guidance'})

    @router.get('/patterns', response_class=HTMLResponse, dependencies=auth)
    def patterns_page(request: Request, status: str='candidate'):
        if status not in PATTERN_STATUSES:
            status = 'candidate'
        return templates.TemplateResponse(request=request, name='patterns.html', context={'patterns': pattern_list(status), 'selected_status': status, 'active': 'patterns'})

    @router.get('/patterns/{pattern_id}', response_class=HTMLResponse, dependencies=auth)
    def pattern_page(request: Request, pattern_id: int):
        rows = db.rows('SELECT * FROM patterns WHERE id=?', (pattern_id,))
        if not rows:
            raise HTTPException(404, 'pattern not found')
        item = _pattern(rows[0], local_tz)
        feedback = db.rows('SELECT action,note,created_at FROM feedback WHERE pattern_id=? ORDER BY id', (pattern_id,))
        for entry in feedback:
            entry['created_at'] = _as_timezone(entry.get('created_at'), local_tz)
        return templates.TemplateResponse(request=request, name='pattern_detail.html', context={'pattern': item, 'feedback': feedback, 'active': 'patterns'})

    @router.get('/memories', response_class=HTMLResponse, dependencies=auth)
    def memories_page(request: Request):
        return templates.TemplateResponse(request=request, name='memories.html', context={'memories': memory_list(status='confirmed'), 'active': 'memories'})

    @router.get('/daily', response_class=HTMLResponse, dependencies=auth)
    async def daily_page(request: Request, day: str | None=None, model: str | None=None):
        listing, detail = summaries('daily_summaries', 'day', day, variant=model)
        return templates.TemplateResponse(request=request, name='summaries.html', context={'title': '日报', 'items': listing, 'selected': detail, 'key': 'day', 'requested': day, 'report_labels': REPORT_FIELD_LABELS, 'report_scope': 'daily', 'non_terminal_statuses': PERIOD_NON_TERMINAL, 'model_options': await available_models(), 'default_model': ollama.model, 'third_party_models': third_party_models(), 'active': 'daily'})

    @router.get('/periods', response_class=HTMLResponse, dependencies=auth)
    async def periods_page(request: Request, period_start: str | None=None, model: str | None=None):
        listing, detail = summaries('period_summaries', 'period_start', period_start, variant=model)
        return templates.TemplateResponse(request=request, name='summaries.html', context={'title': '6小时时段分析', 'items': listing, 'selected': detail, 'key': 'period_start', 'requested': period_start, 'report_labels': REPORT_FIELD_LABELS, 'report_scope': 'period', 'non_terminal_statuses': PERIOD_NON_TERMINAL, 'model_options': await available_models(), 'default_model': ollama.model, 'third_party_models': third_party_models(), 'active': 'periods'})

    @router.get('/weekly', response_class=HTMLResponse, dependencies=auth)
    async def weekly_page(request: Request, week_start: str | None=None, model: str | None=None):
        listing, detail = summaries('weekly_summaries', 'week_start', week_start, variant=model)
        return templates.TemplateResponse(request=request, name='summaries.html', context={'title': '周报', 'items': listing, 'selected': detail, 'key': 'week_start', 'requested': week_start, 'report_labels': REPORT_FIELD_LABELS, 'report_scope': 'weekly', 'non_terminal_statuses': PERIOD_NON_TERMINAL, 'model_options': await available_models(), 'default_model': ollama.model, 'third_party_models': third_party_models(), 'active': 'weekly'})

    @router.get('/suggestions', response_class=HTMLResponse, dependencies=auth)
    def suggestions_page(request: Request):
        return templates.TemplateResponse(request=request, name='suggestions.html', context={'suggestions': suggestion_list(), 'active': 'suggestions'})

    @router.get('/events', response_class=HTMLResponse, dependencies=auth)
    def events_page(request: Request, domain: str | None=None, entity: str | None=None, event_type: str | None=None, from_ts: str | None=None, to_ts: str | None=None, limit: int=Query(50, ge=1, le=200), offset: int=Query(0, ge=0)):
        filters = {'domain': domain or '', 'entity': entity or '', 'event_type': event_type or '', 'from_ts': from_ts or '', 'to_ts': to_ts or ''}
        rows = event_list(domain, entity, event_type, from_ts, to_ts, limit, offset)
        return templates.TemplateResponse(request=request, name='events.html', context={'events': rows, 'filters': filters, 'limit': limit, 'offset': offset, 'active': 'events'})

    @router.get('/sessions', response_class=HTMLResponse, dependencies=auth)
    def sessions_page(request: Request):
        return templates.TemplateResponse(request=request, name='sessions.html', context={'room_sessions': sessions_by_room(), 'active': 'sessions'})
    QUESTION_FLASHES = {'answered': '已保存回答，并已按你选择的范围应用。', 'skipped': '已跳过该问题。', 'invalid': '回答没有保存：请填写内容并选择范围。', 'slot_needs_period': '该问题不是来自时段报告，无法限定到具体时段。', 'already': '该问题已经回答过了。', 'missing': '未找到该问题。', 'queued': '已开始生成问题，完成后会出现在这里。', 'unavailable': '提问功能当前不可用。', 'deferred': '现在处于第三方模型的高峰时段（工作日 09:00–12:00、14:00–18:00），已排入闲时时段自动生成，届时会出现在这里。'}

    def question_scope_options():
        return [(value, ANSWER_SCOPE_LABELS[value]) for value in ('all', 'period', 'daily', 'weekly', 'slot')]

    @router.get('/questions', response_class=HTMLResponse, dependencies=auth)
    async def questions_page(request: Request, status: str | None=None, answers: str | None=None):
        show_all = (answers or '').strip().lower() == 'all'
        if questions is None:
            context = {'pending': [], 'answered': [], 'scope_options': [], 'title': 'AI 提问', 'flash': QUESTION_FLASHES['unavailable'], 'active': 'questions'}
        else:
            total = int(db.scalar("SELECT COUNT(*) FROM ai_questions WHERE status='answered'") or 0)
            context = {'pending': questions.pending(), 'answered': questions.answered(limit=total if show_all else RECENT_ANSWERS), 'answered_total': total, 'showing_all': show_all, 'answers_truncated': total > RECENT_ANSWERS and (not show_all), 'scope_options': question_scope_options(), 'flash': QUESTION_FLASHES.get(status or ''), 'active': 'questions'}
        return templates.TemplateResponse(request=request, name='questions.html', context=context)

    def question_redirect(status):
        return RedirectResponse(f'/questions?status={status}', status_code=303)

    @router.post('/questions/answer', response_class=HTMLResponse, dependencies=auth)
    async def answer_question(request: Request):
        if questions is None:
            return question_redirect('unavailable')
        fields = await form_fields(request)
        try:
            questions.answer(int(fields.get('question_id') or 0), fields.get('answer', ''), fields.get('answer_scope') or 'all')
        except ValueError as error:
            text = str(error)
            status = 'slot_needs_period' if '时段' in text else 'already' if '已经回答' in text else 'missing' if '未找到' in text else 'invalid'
            logger.info('question answer rejected reason=%s', status)
            return question_redirect(status)
        return question_redirect('answered')

    @router.post('/questions/skip', response_class=HTMLResponse, dependencies=auth)
    async def skip_question(request: Request):
        if questions is None:
            return question_redirect('unavailable')
        fields = await form_fields(request)
        try:
            questions.skip(int(fields.get('question_id') or 0))
        except ValueError:
            return question_redirect('missing')
        return question_redirect('skipped')

    @router.post('/questions/generate', response_class=HTMLResponse, dependencies=auth)
    async def generate_questions(request: Request):
        if analyzer is None or questions is None:
            return question_redirect('unavailable')
        if questions.offpeak_only and questions.peak_now():
            key, _, _ = questions.latest_report_key('period')
            questions.defer('period', key)
            logger.info('manual question round deferred to off-peak hours')
            return question_redirect('deferred')
        if not registry.add('questions', 'latest', None, None):
            return question_redirect('queued')
        logger.info('manual question round requested')
        return question_redirect('queued')

    @router.get('/status', response_class=HTMLResponse, dependencies=auth)
    async def status_page(request: Request):
        return templates.TemplateResponse(request=request, name='status.html', context={'status': await status_data(), 'active': 'status'})

    @router.post('/api/maintainers', dependencies=auth)
    def api_record_maintainer(body: MaintainerBody):
        return record_maintenance(body.name, body.note)

    @router.post('/api/maintainers/scores', dependencies=auth)
    def api_record_maintainer_score(body: MaintainerScoreBody):
        return record_maintainer_score(body.target, body.scorer, body.score, body.note)
    return router
