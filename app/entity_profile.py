from pathlib import Path
import yaml
ALLOWED_LAYERS = {'A', 'A/B', 'A/C', 'B', 'C', 'D', 'E'}

class EntityProfile:

    def __init__(self, profile_id, entities):
        self.profile_id = profile_id
        self.entities = entities

    @classmethod
    def from_path(cls, path):
        source = Path(path)
        if not source.is_file():
            raise ValueError(f'entity profile not found: {source}')
        raw = yaml.safe_load(source.read_text(encoding='utf-8'))
        if not isinstance(raw, dict) or raw.get('schema_version') != 1:
            raise ValueError('entity profile requires schema_version: 1')
        profile_id = raw.get('profile_id')
        entities = raw.get('entities')
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ValueError('entity profile requires a non-empty profile_id')
        if not isinstance(entities, dict) or not entities:
            raise ValueError('entity profile requires a non-empty entities mapping')
        normalized = {}
        for entity_id, item in entities.items():
            if not isinstance(entity_id, str) or '.' not in entity_id or (not isinstance(item, dict)):
                raise ValueError(f'invalid entity profile entry: {entity_id!r}')
            if type(item.get('collect')) is not bool:
                raise ValueError(f'entity profile collect must be boolean: {entity_id}')
            if item.get('layer') not in ALLOWED_LAYERS:
                raise ValueError(f'entity profile layer is invalid: {entity_id}')
            normalized[entity_id] = {key: item.get(key) for key in ('collect', 'layer', 'device', 'area', 'category', 'observation_kind')}
        return cls(profile_id.strip(), normalized)

    def collects(self, entity_id):
        item = self.entities.get(entity_id)
        return bool(item and item['collect'])

    def metadata(self, entity_id):
        item = self.entities.get(entity_id)
        if not item:
            return {}
        return {'entity_profile_id': self.profile_id, 'life_layer': item['layer'], 'life_device': item.get('device'), 'life_area': item.get('area'), 'observation_kind': item.get('observation_kind')}
