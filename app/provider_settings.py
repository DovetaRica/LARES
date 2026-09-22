import json
import logging
import os
from pathlib import Path
from .remote_client import OpenAICompatibleClient
logger = logging.getLogger(__name__)
STATE_KEY = 'third_party_config'
KEY_FILENAME = 'third_party.key'

class ProviderSettings:

    def __init__(self, database, data_dir, timeout_seconds=900):
        self.db = database
        self.data_dir = Path(data_dir)
        self.timeout_seconds = timeout_seconds

    @property
    def key_path(self):
        return self.data_dir / KEY_FILENAME

    def _state(self):
        raw = self.db.scalar('SELECT value FROM system_state WHERE key=?', (STATE_KEY,))
        try:
            parsed = json.loads(raw or '{}')
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def load_key(self):
        try:
            return self.key_path.read_text(encoding='utf-8').strip()
        except OSError:
            return ''

    def read(self):
        stored = self._state()
        models = [str(name).strip() for name in stored.get('models') or [] if str(name).strip()]
        return {'enabled': bool(stored.get('enabled')), 'base_url': str(stored.get('base_url') or ''), 'models': models, 'default_model': str(stored.get('default_model') or ''), 'key_present': bool(self.load_key())}

    def save(self, enabled, base_url, models, default_model, api_key=None):
        if api_key is not None:
            self._write_key(api_key.strip())
        names = [str(name).strip() for name in models if str(name).strip()]
        self.db.set_state(STATE_KEY, json.dumps({'enabled': bool(enabled), 'base_url': str(base_url).strip(), 'models': names, 'default_model': str(default_model).strip()}, ensure_ascii=False, separators=(',', ':')))
        logger.info('third-party provider settings saved enabled=%s models=%s key_set=%s', bool(enabled), len(names), bool(self.load_key()))

    def _write_key(self, value):
        if not value:
            try:
                self.key_path.unlink()
            except FileNotFoundError:
                pass
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 384)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(value)
        os.chmod(self.key_path, 384)

    def client(self):
        config = self.read()
        key = self.load_key()
        if not (config['enabled'] and config['base_url'] and key and config['models']):
            return None
        return OpenAICompatibleClient(config['base_url'], key, config['models'], config['default_model'], self.timeout_seconds)

    def automatic_model(self):
        return self.read()['default_model']
