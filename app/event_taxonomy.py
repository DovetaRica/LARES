import json
import logging
from pathlib import Path
logger = logging.getLogger(__name__)
DEVICE_ACTION_DOMAINS = frozenset({'light', 'switch', 'fan', 'climate', 'cover', 'humidifier', 'select', 'button', 'media_player', 'water_heater', 'vacuum', 'number'})
SYSTEM_DOMAINS = frozenset({'system', 'persistent_notification'})
REGISTRY_PATH = (Path(__file__).parent / 'resources' / 'automation_registry.json')
_POLLING_CACHE = None

def classify(event):
    domain = str(event.get('domain') or '').lower()
    event_type = str(event.get('event_type') or '')
    if event_type in ('environment_aggregate', 'appliance_metric_aggregate'):
        return 'sensor_telemetry'
    if domain == 'sensor':
        return 'device_action' if event.get('category') == 'appliance' else 'sensor_telemetry'
    if domain == 'binary_sensor':
        return 'presence_raw'
    if domain == 'automation' or event_type == 'automation_triggered':
        return 'automation_run'
    if domain in SYSTEM_DOMAINS:
        return 'system'
    if domain in DEVICE_ACTION_DOMAINS or event.get('category') == 'appliance':
        return 'device_action'
    if domain in ('person', 'device_tracker', 'event'):
        return 'presence_raw' if domain in ('person', 'device_tracker') else 'device_action'
    return 'device_action'

def _load_registry():
    global _POLLING_CACHE
    if _POLLING_CACHE is None:
        try:
            data = json.loads(REGISTRY_PATH.read_text(encoding='utf-8'))
            _POLLING_CACHE = {item['entity_id']: item for item in data.get('polling', [])}
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.warning('automation registry unavailable (%s): polling labels disabled', type(error).__name__)
            _POLLING_CACHE = {}
    return _POLLING_CACHE

def polling_info(entity_id):
    return _load_registry().get(entity_id)

def reset_registry_cache():
    global _POLLING_CACHE
    _POLLING_CACHE = None
