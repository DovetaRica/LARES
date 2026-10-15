import argparse
import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from .config import settings as default_settings
from .period_recovery import backfill_missing_periods
from .runtime import build_runtime
from .web import create_web_router, run_regeneration_worker
logger = logging.getLogger(__name__)

def log_background_failure(task):
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.exception('Background task failed')

def create_app(settings=None, runtime_factory=build_runtime):
    app_settings = settings if settings is not None else default_settings
    state = {'runtime': None, 'routes': []}
    lifecycle_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        if not lifecycle_lock.acquire(blocking=False):
            raise RuntimeError('application lifespan is already running')
        tasks = []
        scheduler_started = False
        try:
            runtime = runtime_factory(app_settings)
            state['runtime'] = runtime
            app.state.runtime = runtime
            router = create_web_router(runtime.db, runtime.memory, runtime.ha, runtime.ollama, runtime.event_filter, app_settings, cooccurrence=runtime.cooccurrence, analyzer=runtime.analyzer, provider_settings=runtime.provider_settings, questions=runtime.questions, registry=runtime.registry)
            previous = {id(route) for route in state['routes']}
            app.router.routes[:] = [route for route in app.routes if id(route) not in previous]
            retained = {id(route) for route in app.routes}
            app.include_router(router)
            state['routes'] = [route for route in app.routes if id(route) not in retained]
            app.openapi_schema = None
            runtime.arrival_tracker.flush_pending()
            if not app_settings.lares_enable_network or not app_settings.ha_token or app_settings.ha_token.startswith('replace_'):
                logging.getLogger(__name__).error('HA_TOKEN is missing; collector will not start')
            else:
                collector = asyncio.create_task(runtime.ha.run())
                collector.add_done_callback(log_background_failure)
                tasks.append(collector)
            if app_settings.lares_enable_network and app_settings.lares_enable_schedule:
                runtime.scheduler.start()
                scheduler_started = True
                catchup_task = asyncio.create_task(backfill_missing_periods(runtime.analyzer, runtime.registry))
                catchup_task.add_done_callback(log_background_failure)
                tasks.append(catchup_task)
            regeneration_worker = asyncio.create_task(run_regeneration_worker(runtime.analyzer, runtime.registry))
            regeneration_worker.add_done_callback(log_background_failure)
            tasks.append(regeneration_worker)
            yield
        finally:
            try:
                if scheduler_started:
                    runtime.scheduler.shutdown(wait=False)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                finally:
                    state['runtime'] = None
                    app.state.runtime = None
                    lifecycle_lock.release()
    app = FastAPI(title='LARES local application', lifespan=lifespan)
    app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')

    @app.get('/health')
    async def health():
        if state['runtime'] is None:
            return {'status': 'degraded', 'ha': False, 'ollama': False, 'database': False}
        runtime = state['runtime']
        database_ok = True
        try:
            runtime.db.scalar('SELECT 1')
        except Exception:
            database_ok = False
        ollama_ok = await runtime.ollama.healthy()
        status = 'ok' if runtime.ha.connected and ollama_ok and database_ok else 'degraded'
        return {'status': status, 'ha': runtime.ha.connected, 'ollama': ollama_ok, 'database': database_ok}
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from starlette.responses import JSONResponse
    from urllib.parse import urlsplit
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]', 'testserver'])

    @app.middleware('http')
    async def local_request_boundary(request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme != request.url.scheme or parsed.netloc != request.headers.get('host'):
                    return JSONResponse({'detail': 'Cross-origin write blocked'}, status_code=403)
            elif request.headers.get('x-lares-client') != 'local':
                return JSONResponse({'detail': 'Same-origin request or X-LARES-Client: local required'}, status_code=403)
        return await call_next(request)
    return app
app = create_app()


def main():
    parser = argparse.ArgumentParser(description='LARES local application preview')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--demo', action='store_true', help='Temporary synthetic data; networking disabled')
    args = parser.parse_args()
    import uvicorn
    if args.demo:
        import os
        import tempfile
        import logging
        from pathlib import Path
        from .database import Database
        from .demo import seed
        with tempfile.TemporaryDirectory(prefix='lares-preview-') as directory:
            path = Path(directory)
            os.environ.update(DATABASE_PATH=str(path/'demo.db'), LOG_PATH=str(path/'demo.log'),
                              BACKUP_DIR=str(path/'backups'), LARES_ENABLE_NETWORK='false',
                              LARES_ENABLE_SCHEDULE='false', THIRD_PARTY_ENABLED='false',
                              HA_TOKEN='', LEARNING_ONLY='true')
            seed(Database(path/'demo.db'))
            try:
                from .config import Settings
                resources = Path(__file__).parent / 'resources'
                demo_settings = Settings(
                    _env_file=None, database_path=path/'demo.db', log_path=path/'demo.log',
                    backup_dir=path/'backups', config_path=resources/'entities.yaml',
                    home_ai_entity_profile_path=resources/'home_ai_entities.proposed.yaml',
                    home_ai_entity_profile_enabled=False, lares_enable_network=False,
                    lares_enable_schedule=False, third_party_enabled=False, ha_url='', ha_token='',
                    sleep_curtain_entity='', sleep_bed_entities='', sleep_room='Example room',
                    arrival_outside_entity='', arrival_entry_entity='', arrival_entry_door_entity='',
                    presence_noise_entities='', presence_non_occupancy_entities='',
                    learning_only=True, proactive_enabled=False, report_review_enabled=False)
                uvicorn.run(create_app(settings=demo_settings), host='127.0.0.1', port=args.port)
            finally:
                logging.shutdown()
    else:
        uvicorn.run('app.main:create_app', factory=True, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
