from datetime import datetime, timedelta, timezone
from app.database import Database
from app.memory_manager import MemoryManager

def test_harvest_session_habits_writes_memory_and_suggestion(tmp_path):
    db = Database(tmp_path / 't.db')
    base = datetime.now(timezone.utc) - timedelta(days=5)
    with db.connect() as con:
        for day in range(7):
            start = (base + timedelta(days=day)).isoformat()
            end = (base + timedelta(days=day, minutes=45)).isoformat()
            con.execute("INSERT INTO sessions(room,type,start_ts,end_ts,duration_seconds,status,evidence_json) VALUES('客厅','room_activity',?,?,2700,'closed','[]')", (start, end))
        con.execute("INSERT INTO sessions(room,type,start_ts,end_ts,duration_seconds,status,evidence_json) VALUES('次卧','room_activity',?,?,120,'closed','[]')", (base.isoformat(), (base + timedelta(minutes=2)).isoformat()))
    mem = MemoryManager(db, min_pattern_samples=5)
    created, suggestions = mem.harvest_session_habits(lookback_days=21, min_days=3, min_minutes_per_day=20)
    assert created, 'multi-day room occupancy must become at least one memory'
    room = created[0][1]
    assert room == '客厅'
    rows = db.rows("SELECT type,content,status FROM memories WHERE type='room_habit'")
    assert rows and rows[0]['status'] == 'observed'
    assert '客厅' in rows[0]['content']
    assert db.rows('SELECT id FROM automation_suggestions'), 'habit room should propose one suggestion'
    again, again_s = mem.harvest_session_habits()
    assert again == [] and again_s == []

def test_cooccurrence_description_is_not_vacuum_template():
    from pathlib import Path
    src = Path('app/llm_schema.py').read_text(encoding='utf-8')
    assert '也不能说明本次实际清扫房间' not in src
    assert '相关非因果' in src

def test_question_schema_accepts_options(tmp_path):
    from app.llm_schema import validate_question_response
    items = validate_question_response({'questions': [{'question': '这是定时计划吗？', 'why': '报告只在清晨出现一次运行', 'options': ['定时计划', '不是定时', '不确定']}]})
    assert items[0]['options'] == ['定时计划', '不是定时', '不确定']
