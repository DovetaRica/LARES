import asyncio
import logging
from datetime import date, datetime, timezone
logger = logging.getLogger(__name__)

def _utcnow():
    return datetime.now(timezone.utc).isoformat()

class RegenerationRegistry:

    def __init__(self, db=None):
        self.db = db
        self.queue = asyncio.Queue()
        self.jobs = []
        self.running = None
        self.error = None
        if db is not None:
            self._restore()

    def _restore(self):
        with self.db.connect() as con:
            con.execute("UPDATE manual_jobs SET status='interrupted', error_type='AppRestart', finished_at=? WHERE status='running'", (_utcnow(),))
            rows = con.execute("SELECT id,scope,key,value,model,queued_at FROM manual_jobs WHERE status='queued' ORDER BY id").fetchall()
        for row in rows:
            self._enqueue(dict(row))

    @property
    def in_flight(self):
        return {(job['scope'], job['key']) for job in self.jobs}

    def _enqueue(self, job):
        self.jobs.append(job)
        self.queue.put_nowait(job)

    def add(self, scope, key, value, model=None):
        if any((job['scope'] == scope and job['key'] == key for job in self.jobs)):
            return False
        queued_at = _utcnow()
        job = {'scope': scope, 'key': key, 'value': value, 'model': model, 'queued_at': queued_at}
        if self.db is not None:
            with self.db.connect() as con:
                cursor = con.execute("INSERT INTO manual_jobs(scope,key,value,model,status,queued_at) VALUES(?,?,?,?,'queued',?) ON CONFLICT(scope,key) WHERE status IN ('queued','running') DO NOTHING", (scope, key, None if value is None else str(value), model, queued_at))
                if cursor.rowcount == 0:
                    return False
                job['id'] = cursor.lastrowid
        self._enqueue(job)
        return True

    def mark_running(self, job):
        if self.db is None:
            return
        with self.db.connect() as con:
            cursor = con.execute("UPDATE manual_jobs SET status='running', started_at=? WHERE id=? AND status='queued'", (_utcnow(), job['id']))
            if cursor.rowcount != 1:
                raise RuntimeError('manual job is no longer queued')

    def finish(self, job, status, error_type=None):
        if self.db is None:
            return True
        try:
            with self.db.connect() as con:
                cursor = con.execute("UPDATE manual_jobs SET status=?, finished_at=?, error_type=? WHERE id=? AND status IN ('queued','running')", (status, _utcnow(), error_type, job['id']))
                if cursor.rowcount != 1:
                    raise RuntimeError('manual job has no active row')
        except Exception as error:
            self.error = {'code': 'terminal_save_failed', 'error_type': type(error).__name__}
            logger.error('manual job terminal state failed error=%s', type(error).__name__)
            return False
        return True

    def snapshot(self):
        return {'running': dict(self.running) if self.running else None, 'error': dict(self.error) if self.error else None, 'waiting': [dict(job) for job in self.jobs if job is not self.running], 'recent': self.db.rows("SELECT scope,key,value,model,status,queued_at,finished_at,error_type FROM manual_jobs WHERE status NOT IN ('queued','running') ORDER BY id DESC LIMIT 10") if self.db else []}

async def run_regeneration_worker(analyzer, registry):
    while True:
        job = await registry.queue.get()
        registry.running = job
        terminal = False
        try:
            registry.mark_running(job)
            value, model = (job['value'], job['model'])
            if job['scope'] == 'period':
                await analyzer.period(end=datetime.fromisoformat(str(value)), source='manual', model=model)
            elif job['scope'] == 'daily':
                await analyzer.daily(day=date.fromisoformat(str(value)[:10]), model=model)
            elif job['scope'] == 'weekly':
                await analyzer.weekly(week_start=date.fromisoformat(str(value)[:10]), model=model)
            elif job['scope'] == 'questions':
                await analyzer.ask_questions('period', model=model)
            elif job['scope'] == 'distill':
                report_scope, _, report_key = str(job['value']).partition(':')
                await analyzer.distill(report_scope or 'period', report_key)
            else:
                raise ValueError('unknown task scope')
        except asyncio.CancelledError:
            terminal = registry.finish(job, 'interrupted', 'CancelledError')
            raise
        except Exception as error:
            logger.error('manual report regeneration failed error=%s', type(error).__name__)
            terminal = registry.finish(job, 'failed', type(error).__name__)
        else:
            terminal = registry.finish(job, 'completed')
        finally:
            registry.running = None
            if terminal:
                registry.jobs = [item for item in registry.jobs if item is not job]
            registry.queue.task_done()
        if not terminal:
            raise RuntimeError('manual job terminal state was not persisted')
