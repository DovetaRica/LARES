"""Offline application check; run from any directory in an app-enabled environment."""
import os
import socket
import tempfile
import asyncio
import logging
from pathlib import Path


async def check():
    # Fail at the socket boundary even if a future transport bypasses our policy.
    def deny(*args, **kwargs):
        raise AssertionError('Network is forbidden in the application smoke test')
    socket.socket.connect = deny
    socket.create_connection = deny
    os.environ.update(LARES_ENABLE_NETWORK='false', LARES_ENABLE_SCHEDULE='false',
                      HA_TOKEN='', THIRD_PARTY_ENABLED='false')
    with tempfile.TemporaryDirectory(prefix='lares-app-smoke-') as folder:
        previous = Path.cwd()
        os.chdir(folder)
        os.environ.update(DATABASE_PATH=str(Path(folder)/'example.db'),
                          LOG_PATH=str(Path(folder)/'example.log'), BACKUP_DIR=str(Path(folder)/'backups'))
        # A user allowlist does not need to duplicate packaged model prompts.
        config = Path(folder)/'entities.yaml'
        config.write_text('domains: [light]\ninclude_entities: [light.example_lamp]\n', encoding='utf-8')
        os.environ['CONFIG_PATH'] = str(config)
        try:
            import httpx
            from app.main import create_app
            application = create_app()
            async with application.router.lifespan_context(application), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url='http://testserver'
            ) as client:
                pages = ['/', '/guidance', '/periods', '/daily', '/weekly', '/patterns',
                         '/memories', '/suggestions', '/sessions', '/events', '/status',
                         '/settings', '/questions', '/health']
                for page in pages:
                    response = await client.get(page)
                    assert response.status_code == 200, (page, response.status_code)
                assert (await client.get('/', headers={'host': 'unexpected.example'})).status_code == 400
                assert (await client.post('/api/maintainers', json={'name': 'synthetic-test'},
                                   headers={'origin': 'https://unexpected.example'})).status_code == 403
                assert (await client.post('/api/maintainers', json={'name': 'synthetic-test'})).status_code == 403
                assert (await client.post('/api/maintainers', json={'name': 'synthetic-test'},
                                   headers={'origin': 'http://testserver'})).status_code == 200
                from app.runtime import db, scheduler
                assert not scheduler.running
                assert db.scalar('SELECT COUNT(*) FROM model_calls') == 0
                assert db.scalar('SELECT COUNT(*) FROM movements') == 0
            print('PASS: 14 offline routes; Host and cross-origin write boundaries; no network')
        finally:
            logging.shutdown()
            os.chdir(previous)


if __name__ == '__main__':
    asyncio.run(check())
