import hashlib
from datetime import datetime
from pathlib import Path
import yaml
from .entity_profile import EntityProfile
from .event_taxonomy import classify as classify_metric
from .automation_corrections import control_attributes
IMPORTANT_DOMAINS = {'light', 'switch', 'climate', 'cover', 'media_player', 'scene', 'script', 'automation'}

class EventFilter:

    def __init__(self, config_path, presence_debounce_seconds=30, profile_path=None, profile_enabled=False, opening_debounce_seconds=2):
        path = Path(config_path)
        self.cfg = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
        self.include = set(self.cfg.get('include_entities') or [])
        self.exclude = set(self.cfg.get('exclude_entities') or [])
        self.domains = set(self.cfg.get('domains') or [])
        self.env_classes = set(self.cfg.get('environment', {}).get('device_classes') or [])
        self.env_keywords = tuple((str(item).lower() for item in self.cfg.get('environment', {}).get('entity_keywords') or []))
        appliance_cfg = self.cfg.get('appliances', {})
        self.appliance_domains = set(appliance_cfg.get('domains') or [])
        self.appliance_keywords = {kind: tuple((str(item).lower() for item in keywords)) for kind, keywords in (appliance_cfg.get('keywords') or {}).items()}
        self.presence_classes = set(self.cfg.get('presence', {}).get('device_classes') or [])
        self.opening_classes = set(self.cfg.get('doors_windows', {}).get('device_classes') or [])
        self.debounce = presence_debounce_seconds
        self.opening_debounce = opening_debounce_seconds
        self.last_presence = {}
        self.last_opening = {}
        self.last_automation = {}
        self.area_names = {}
        self.profile = EntityProfile.from_path(profile_path) if profile_enabled else None

    def set_area_names(self, area_names):
        self.area_names = {str(key): str(value)[:80] for key, value in (area_names or {}).items() if key and value}

    def accepts_entity(self, entity_id):
        if not entity_id or entity_id in self.exclude:
            return False
        if self.profile:
            return self.profile.collects(entity_id)
        if self.include and entity_id not in self.include:
            return False
        return entity_id.split('.', 1)[0] in self.domains

    def accepts_state(self, entity_id, attributes=None):
        if not self.accepts_entity(entity_id):
            return False
        if self.profile:
            return True
        if entity_id in self.include:
            return True
        domain = entity_id.split('.', 1)[0]
        attributes = attributes or {}
        device_class = attributes.get('device_class')
        appliance_type = self._appliance_type(entity_id, attributes)
        if domain == 'sensor':
            return device_class in self.env_classes or self._is_environment_keyword(entity_id, attributes) or bool(appliance_type)
        if domain == 'binary_sensor':
            return device_class in self.presence_classes or device_class in self.opening_classes or bool(appliance_type)
        if appliance_type and domain in self.appliance_domains:
            return True
        return domain in IMPORTANT_DOMAINS or domain in {'person', 'device_tracker'}

    @staticmethod
    def _search_text(entity_id, attributes):
        return f"{entity_id} {(attributes or {}).get('friendly_name', '')}".lower()

    def _is_environment_keyword(self, entity_id, attributes):
        text = self._search_text(entity_id, attributes)
        return any((keyword in text for keyword in self.env_keywords))

    def _appliance_type(self, entity_id, attributes):
        text = self._search_text(entity_id, attributes)
        if self.profile:
            profile_device = self.profile.metadata(entity_id).get('life_device')
            if profile_device:
                text = f'{text} {profile_device}'.lower()
        for kind, keywords in self.appliance_keywords.items():
            if any((keyword in text for keyword in keywords)):
                return kind
        return None

    def _environment_type(self, entity_id, attributes):
        device_class = str((attributes or {}).get('device_class') or '').lower()
        if device_class in self.env_classes:
            return 'carbon_dioxide' if device_class == 'co2' else device_class
        text = self._search_text(entity_id, attributes)
        if any((item in text for item in ('hcho_density', 'formaldehyde', '甲醛浓度'))):
            return 'formaldehyde'
        if any((item in text for item in ('co2_density', 'carbon_dioxide', 'co2浓度', '二氧化碳'))):
            return 'carbon_dioxide'
        return None

    def coverage(self, states):
        result = {'appliances': {}, 'environment': {}}
        for state in states:
            entity_id = state.get('entity_id')
            attributes = state.get('attributes') or {}
            if not self.accepts_state(entity_id, attributes):
                continue
            available = state.get('state') not in {'unavailable', 'unknown'}
            for section, kind in (('appliances', self._appliance_type(entity_id, attributes)), ('environment', self._environment_type(entity_id, attributes))):
                if kind:
                    counts = result[section].setdefault(kind, {'monitored': 0, 'available': 0})
                    counts['monitored'] += 1
                    counts['available'] += int(available)
        return result

    @staticmethod
    def _numeric(value):
        try:
            float(value)
            return True
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _is_presence_duration_sensor(entity_id, attributes=None):
        text = str(entity_id or '').lower()
        name = str((attributes or {}).get('friendly_name') or '')
        if 'no_one_duration' in text or 'has_someone_duration' in text:
            return True
        return '持续时长' in name and 'duration' in text

    @staticmethod
    def _user_hash(user_id):
        return hashlib.sha256(user_id.encode()).hexdigest()[:16] if user_id else None

    def classify_state_changed(self, data, event_time):
        entity_id = data.get('entity_id')
        new = data.get('new_state') or {}
        old = data.get('old_state') or {}
        domain = entity_id.split('.', 1)[0]
        attrs = new.get('attributes') or {}
        if not self.accepts_state(entity_id, attrs):
            return None
        device_class = attrs.get('device_class')
        appliance_type = self._appliance_type(entity_id, attrs)
        context = new.get('context') or data.get('context') or {}
        user_id = context.get('user_id')
        source = 'manual' if user_id else 'automation' if context.get('parent_id') else 'system'
        if domain == 'automation':
            return None
        if source == 'system' and old.get('state') == new.get('state'):
            return None
        category = 'device'
        if device_class in self.env_classes or (domain == 'sensor' and self._is_environment_keyword(entity_id, attrs)):
            category = 'environment'
        elif domain == 'sensor' and self._is_presence_duration_sensor(entity_id, attrs):
            category = 'appliance_metric'
        elif appliance_type and domain == 'sensor' and self._numeric(new.get('state')):
            category = 'appliance_metric'
        elif appliance_type:
            category = 'appliance'
        elif device_class in self.presence_classes or domain in {'person', 'device_tracker'}:
            category = 'presence'
        elif device_class in self.opening_classes:
            category = 'opening'
        elif domain == 'media_player':
            category = 'media'
        ts = datetime.fromisoformat(event_time.replace('Z', '+00:00'))
        if category == 'presence':
            new_state = str(new.get('state'))
            old_state = old.get('state')
            if old_state is not None and str(old_state) == new_state:
                return None
            last = self.last_presence.get(entity_id)
            if last:
                dt = (ts - last[0]).total_seconds()
                if last[1] == new_state and 0 <= dt < self.debounce:
                    return None
                if ts >= last[0]:
                    self.last_presence[entity_id] = (ts, new_state)
            else:
                self.last_presence[entity_id] = (ts, new_state)
        elif category == 'opening':
            last_open = self.last_opening.get(entity_id)
            if last_open is not None:
                dt = (ts - last_open).total_seconds()
                if 0 <= dt < self.opening_debounce:
                    return None
                if ts >= last_open:
                    self.last_opening[entity_id] = ts
            else:
                self.last_opening[entity_id] = ts
        metadata = {'appliance_type': appliance_type} if appliance_type else {}
        if self.profile:
            metadata.update(self.profile.metadata(entity_id))
        metadata['metric_class'] = classify_metric({'domain': domain, 'event_type': 'state_changed', 'category': category})
        return {'ts': event_time, 'entity_id': entity_id, 'domain': domain, 'category': category, 'event_type': 'state_changed', 'old_state': old.get('state'), 'new_state': new.get('state'), 'source': source, 'context_id': context.get('id'), 'parent_id': context.get('parent_id'), 'user_id_hash': self._user_hash(user_id), 'area': attrs.get('area_id'), 'attributes_json': {**{k: attrs.get(k) for k in ('brightness', 'color_temp_kelvin', 'temperature', 'current_temperature', 'humidity', 'percentage', 'fan_mode', 'preset_mode', 'device_class', 'friendly_name', 'unit_of_measurement') if k in attrs}, **control_attributes(domain, attrs)}, 'metadata_json': metadata}

    def classify_event(self, message):
        event = message.get('event') or {}
        event_type, data = (event.get('event_type'), event.get('data') or {})
        ts = event.get('time_fired') or datetime.utcnow().isoformat() + 'Z'
        if event_type == 'state_changed':
            return self.classify_state_changed(data, ts)
        if event_type == 'automation_triggered':
            entity_id = data.get('entity_id')
            if self.profile and (not self.profile.collects(entity_id)):
                return None
            now = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            prior = self.last_automation.get(entity_id)
            if prior:
                dt = (now - prior).total_seconds()
                if 0 <= dt < self.debounce:
                    return None
                if now >= prior:
                    self.last_automation[entity_id] = now
            else:
                self.last_automation[entity_id] = now
            return {'ts': ts, 'entity_id': data.get('entity_id'), 'domain': 'automation', 'category': 'automation', 'event_type': event_type, 'old_state': None, 'new_state': 'triggered', 'source': 'automation', 'context_id': (event.get('context') or {}).get('id'), 'parent_id': (event.get('context') or {}).get('parent_id'), 'user_id_hash': None, 'area': None, 'attributes_json': {}, 'metadata_json': {'name': data.get('name'), 'metric_class': 'automation_run'}}
        if event_type == 'call_service' and data.get('domain') == 'vacuum' and (data.get('service') == 'clean_area'):
            service_data = data.get('service_data') if isinstance(data.get('service_data'), dict) else {}
            target = data.get('target') if isinstance(data.get('target'), dict) else {}

            def values(value):
                if isinstance(value, str):
                    return [value]
                if isinstance(value, (list, tuple)):
                    return [str(item) for item in value if item]
                return []
            entity_ids = values(service_data.get('entity_id')) or values(target.get('entity_id'))
            entity_id = next((item for item in entity_ids if item.startswith('vacuum.') and self.accepts_entity(item)), None)
            if not entity_id:
                return None
            area_ids = values(service_data.get('cleaning_area_id')) or values(service_data.get('area_id'))
            area_names = [self.area_names.get(area_id, area_id) for area_id in area_ids][:16]
            context = event.get('context') or {}
            user_id = context.get('user_id')
            source = 'manual' if user_id else 'automation' if context.get('parent_id') else 'system'
            readable = '、'.join(area_names)
            metadata = {'appliance_type': 'vacuum', 'cleaning_area_ids': area_ids[:16], 'cleaning_area_names': area_names, 'metric_class': 'device_action', 'observation_limit': '记录的是 Home Assistant 发出的区域清扫请求，不代表任务已经完成'}
            if self.profile:
                metadata.update(self.profile.metadata(entity_id))
            return {'ts': ts, 'entity_id': entity_id, 'domain': 'vacuum', 'category': 'appliance', 'event_type': 'vacuum_area_clean_requested', 'old_state': None, 'new_state': f'请求清扫：{readable}' if readable else '已请求区域清扫', 'source': source, 'context_id': context.get('id'), 'parent_id': context.get('parent_id'), 'user_id_hash': self._user_hash(user_id), 'area': area_ids[0] if len(area_ids) == 1 else None, 'attributes_json': {}, 'metadata_json': metadata}
        return None
