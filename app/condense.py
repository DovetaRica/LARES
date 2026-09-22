import json
import logging
from datetime import datetime, timezone
logger = logging.getLogger(__name__)
ALLOWED_SCOPES = ('all', 'period', 'daily', 'weekly')
MAX_RULES = 40
MAX_RULE_CHARS = 1000
DEFAULT_LIMIT = 200

def _now():
    return datetime.now(timezone.utc).isoformat()

def input_bundle(db, limit=DEFAULT_LIMIT):
    guidance = db.rows('SELECT id,content,scope,source,created_at FROM ai_guidance WHERE active=1 ORDER BY id', ())
    archived_count = int(db.scalar('SELECT COUNT(*) FROM ai_guidance WHERE active=0') or 0)
    questions = db.rows('SELECT id,question,why,status,answer,scope_key,created_at FROM ai_questions ORDER BY id DESC LIMIT ?', (min(max(int(limit), 1), 500),))
    return {'generated_at': _now(), 'counts': {'active_guidance': len(guidance), 'archived_guidance': archived_count, 'questions': len(questions)}, 'rules_in_effect': [{'id': row['id'], 'content': row['content'], 'scope': row['scope'], 'source': row['source'], 'created_at': row['created_at']} for row in guidance], 'questions': [{'id': row['id'], 'question': row['question'], 'why': row['why'], 'status': row['status'], 'answer': row['answer'], 'report': row['scope_key'], 'created_at': row['created_at']} for row in questions], 'how_to_submit': {'endpoint': 'POST /api/condense/proposal', 'body': {'external_id': 'your-id', 'source': 'your-name', 'notes': 'optional summary of what you merged and why', 'rules': [{'text': 'one durable fact or preference', 'scope': 'all', 'replaces': [12, 34]}]}, 'note': 'rules 里每条只写一件事；replaces 填被它取代的 rule id（可留空）。业主批准后系统会插入这些规则并归档被取代的条目（不删除）。'}}

class CondenseValidationError(ValueError):
    pass

def _clean_rules(raw):
    if not isinstance(raw, list) or not raw:
        raise CondenseValidationError('rules 必须是非空数组')
    if len(raw) > MAX_RULES:
        raise CondenseValidationError(f'rules 最多 {MAX_RULES} 条')
    cleaned = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CondenseValidationError(f'第 {index} 条规则不是对象')
        text = str(item.get('text') or '').strip()
        if not text:
            raise CondenseValidationError(f'第 {index} 条规则缺少 text')
        if len(text) > MAX_RULE_CHARS:
            raise CondenseValidationError(f'第 {index} 条规则超过 {MAX_RULE_CHARS} 字')
        scope = str(item.get('scope') or 'all').strip().lower()
        if scope not in ALLOWED_SCOPES:
            scope = 'all'
        replaces = []
        for value in item.get('replaces') or []:
            try:
                replaces.append(int(value))
            except (TypeError, ValueError):
                continue
        cleaned.append({'text': text, 'scope': scope, 'replaces': sorted(set(replaces))})
    return cleaned

def submit(db, source, external_id, rules, notes=None):
    external_id = str(external_id or '').strip()
    if not external_id:
        raise CondenseValidationError('external_id 不能为空')
    source = str(source or '').strip()
    if not source:
        raise CondenseValidationError('source 不能为空')
    cleaned = _clean_rules(rules)
    existing = db.rows('SELECT id,status FROM condense_proposals WHERE external_id=?', (external_id,))
    if existing:
        return {'accepted': True, 'duplicate': True, 'id': existing[0]['id'], 'status': existing[0]['status']}
    with db.connect() as con:
        cursor = con.execute('INSERT INTO condense_proposals(external_id,source,rules_json,notes,created_at) VALUES(?,?,?,?,?)', (external_id, source, json.dumps(cleaned, ensure_ascii=False), str(notes or '').strip()[:2000] or None, _now()))
    logger.info('condense proposal received source=%s rules=%s', source, len(cleaned))
    return {'accepted': True, 'duplicate': False, 'id': cursor.lastrowid, 'status': 'pending', 'rules': len(cleaned)}

def _stored(row):
    try:
        rules = json.loads(row.get('rules_json') or '[]')
    except (TypeError, ValueError):
        rules = []
    try:
        applied = json.loads(row.get('applied_json') or 'null')
    except (TypeError, ValueError):
        applied = None
    replaced = sorted({int(value) for rule in rules if isinstance(rule, dict) for value in rule.get('replaces') or []})
    detail = dict(row)
    detail['rules'] = rules if isinstance(rules, list) else []
    detail['replaced_ids'] = replaced
    detail['applied'] = applied
    return detail

def proposals(db, status=None, limit=50):
    where, params = ('', ())
    if status:
        where, params = ('WHERE status=?', (str(status),))
    rows = db.rows(f'SELECT * FROM condense_proposals {where} ORDER BY id DESC LIMIT ?', params + (min(max(int(limit), 1), 200),))
    return [_stored(row) for row in rows]

def proposal(db, proposal_id):
    rows = db.rows('SELECT * FROM condense_proposals WHERE id=?', (proposal_id,))
    return _stored(rows[0]) if rows else None

def approve(db, proposal_id):
    stored = proposal(db, proposal_id)
    if stored is None:
        raise CondenseValidationError('未找到合并提案')
    if stored['status'] != 'pending':
        raise CondenseValidationError('该提案已经审核过')
    now = _now()
    inserted, archived = ([], [])
    with db.connect() as con:
        for rule in stored['rules']:
            cursor = con.execute('INSERT INTO ai_guidance(content,scope,source,created_at) VALUES(?,?,?,?)', (rule['text'], rule.get('scope') or 'all', stored['source'], now))
            inserted.append(cursor.lastrowid)
        for guidance_id in stored['replaced_ids']:
            cursor = con.execute('UPDATE ai_guidance SET active=0, archived_at=? WHERE id=? AND active=1', (now, guidance_id))
            if cursor.rowcount:
                archived.append(guidance_id)
        con.execute("UPDATE condense_proposals SET status='approved',reviewed_at=?,guidance_ids=?,applied_json=? WHERE id=?", (now, json.dumps(inserted), json.dumps({'inserted': inserted, 'archived': archived}, ensure_ascii=False), proposal_id))
    logger.info('condense proposal applied id=%s inserted=%s archived=%s', proposal_id, len(inserted), len(archived))
    return {'ok': True, 'id': proposal_id, 'status': 'approved', 'inserted': inserted, 'archived': archived}

def reject(db, proposal_id):
    stored = proposal(db, proposal_id)
    if stored is None:
        raise CondenseValidationError('未找到合并提案')
    if stored['status'] != 'pending':
        raise CondenseValidationError('该提案已经审核过')
    with db.connect() as con:
        con.execute("UPDATE condense_proposals SET status='rejected',reviewed_at=? WHERE id=?", (_now(), proposal_id))
    return {'ok': True, 'id': proposal_id, 'status': 'rejected'}
