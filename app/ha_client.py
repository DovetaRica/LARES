import asyncio
import json
import logging
from datetime import datetime, timezone
import websockets
from .network import check_endpoint
log = logging.getLogger(__name__)
SNAPSHOT_MAX_ENTITIES = 80
SNAPSHOT_ATTRS = ('brightness', 'temperature', 'current_temperature', 'humidity', 'percentage', 'unit_of_measurement')
MOVING_APPLIANCE_TYPES = frozenset({'vacuum', 'washing_machine', 'dryer'})

class HAClient:

    def __init__(self, url, token, event_filter, aggregator, sessions, database, sleep_tracker=None, arrival_tracker=None):
        self.ws_url = url.rstrip('/').replace('http://', 'ws://').replace('https://', 'wss://') + '/api/websocket'
        self.token, self.filter, self.aggregator, self.sessions, self.db = (token, event_filter, aggregator, sessions, database)
        self.sleep_tracker = sleep_tracker
        self.arrival_tracker = arrival_tracker
        self.connected = False
        self._stop = False
        self.vacuum_segments = {}

    async def _connect(self):
        check_endpoint(self.ws_url.replace('wss://', 'https://').replace('ws://', 'http://'))
        connection = websockets.connect(self.ws_url, open_timeout=15, ping_interval=30,
                                        max_size=1048576, max_queue=16, proxy=None)
        connection.process_redirect = lambda error: error
        return await connection

    async def _auth(self, ws):
        hello = json.loads(await ws.recv())
        if hello.get('type') != 'auth_required':
            raise RuntimeError('unexpected HA WebSocket greeting')
        await ws.send(json.dumps({'type': 'auth', 'access_token': self.token}))
        auth = json.loads(await ws.recv())
        if auth.get('type') != 'auth_ok':
            raise RuntimeError('Home Assistant authentication failed')

    async def _refresh_vacuum_context(self, ws, states, first_id=10):
        area_names = {}
        command_id = first_id
        await ws.send(json.dumps({'id': command_id, 'type': 'config/area_registry/list'}))
        area_result = json.loads(await ws.recv())
        if area_result.get('success') and isinstance(area_result.get('result'), list):
            area_names = {str(item.get('area_id')): str(item.get('name'))[:80] for item in area_result['result'] if isinstance(item, dict) and item.get('area_id') and item.get('name')}
            self.filter.set_area_names(area_names)
        vacuum_entities = []
        for state in states or []:
            entity_id = str(state.get('entity_id') or '')
            if entity_id.startswith('vacuum.') and self.filter.accepts_state(entity_id, state.get('attributes') or {}):
                vacuum_entities.append(entity_id)
        capabilities = {}
        for entity_id in vacuum_entities[:8]:
            command_id += 1
            await ws.send(json.dumps({'id': command_id, 'type': 'vacuum/get_segments', 'entity_id': entity_id}))
            result = json.loads(await ws.recv())
            raw_segments = result.get('result') if result.get('success') else None
            if isinstance(raw_segments, dict):
                raw_segments = raw_segments.get('segments')
            if not isinstance(raw_segments, list):
                continue
            segments = []
            for item in raw_segments[:32]:
                if not isinstance(item, dict) or item.get('id') is None:
                    continue
                name = str(item.get('name') or '').strip()
                if name:
                    segments.append({'id': str(item['id'])[:40], 'name': name[:80]})
            if segments:
                capabilities[entity_id] = segments
        self.vacuum_segments = capabilities
        self.db.set_state('vacuum_room_capabilities', json.dumps({'updated_at': datetime.now(timezone.utc).isoformat(), 'vacuums': [{'entity_id': entity_id, 'segments': segments} for entity_id, segments in capabilities.items()]}, ensure_ascii=False, separators=(',', ':')))
        return command_id + 1

    async def run(self):
        delay = 2
        while not self._stop:
            try:
                async with await self._connect() as ws:
                    await self._auth(ws)
                    await ws.send(json.dumps({'id': 1, 'type': 'get_states'}))
                    states_result = json.loads(await ws.recv())
                    if states_result.get('success'):
                        states = states_result.get('result', [])
                        monitored = sum((1 for state in states if self.filter.accepts_state(state.get('entity_id'), state.get('attributes') or {})))
                        self.db.set_state('monitored_entity_count', monitored)
                        self.db.set_state('entity_coverage', json.dumps(self.filter.coverage(states), ensure_ascii=False))
                        if self.sleep_tracker is not None:
                            self.sleep_tracker.seed_state(states)
                        if self.arrival_tracker is not None:
                            self.arrival_tracker.seed_state(states)
                        if self.sessions is not None:
                            self.sessions.seed_state(states, getattr(self.filter, 'presence_classes', ()), getattr(self.filter, 'profile', None))
                        subscription_id = await self._refresh_vacuum_context(ws, states)
                    else:
                        subscription_id = 2
                    await ws.send(json.dumps({'id': subscription_id, 'type': 'subscribe_events'}))
                    subscription = json.loads(await ws.recv())
                    if not subscription.get('success'):
                        raise RuntimeError('Home Assistant event subscription failed')
                    self.connected, delay = (True, 2)
                    self.db.set_state('ha_connected', 'true')
                    log.info('Home Assistant WebSocket connected')
                    async for raw in ws:
                        msg = json.loads(raw)
                        event = self.filter.classify_event(msg)
                        if event:
                            self.aggregator.process(event)
                            self.sessions.process(event)
                            if self.sleep_tracker is not None:
                                self.sleep_tracker.process(event)
                            if self.arrival_tracker is not None:
                                self.arrival_tracker.process(event)
                            self.db.set_state('last_ha_event', event['ts'])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.db.set_state('ha_connected', 'false')
                log.warning('Home Assistant connection unavailable: %s', type(exc).__name__)
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)

    async def current_states(self):
        try:
            async with await self._connect() as ws:
                await self._auth(ws)
                await ws.send(json.dumps({'id': 1, 'type': 'get_states'}))
                result = json.loads(await ws.recv())
                if result.get('success') and isinstance(result.get('result'), list):
                    try:
                        await self._refresh_vacuum_context(ws, result['result'], first_id=2)
                    except Exception as exc:
                        log.warning('vacuum room snapshot unavailable: %s', type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('current-state snapshot unavailable: %s', type(exc).__name__)
            return None
        if not result.get('success'):
            return None
        return result.get('result', [])

    def compact_snapshot(self, states):
        prioritized, ordinary = ([], [])
        for state in states or []:
            entity_id = state.get('entity_id') or ''
            attributes = state.get('attributes') or {}
            if not self.filter.accepts_state(entity_id, attributes):
                continue
            item = {'entity_id': entity_id, 'state': state.get('state')}
            appliance_type = self.filter._appliance_type(entity_id, attributes)
            profile = getattr(self.filter, 'profile', None)
            identity = profile.metadata(entity_id) if profile is not None else {}
            device = str(identity.get('life_device') or '').strip()
            if device == 'Robotic Vacuum Cleaner':
                device = '扫地机器人'
            friendly_name = str(attributes.get('friendly_name') or '').strip()
            if device:
                item['device'] = device[:120]
            elif friendly_name:
                item['name'] = friendly_name[:120]
            area = str(identity.get('life_area') or '').strip()
            if area:
                item['area'] = area[:80]
            if appliance_type:
                item['appliance_type'] = appliance_type[:64]
            if entity_id in self.vacuum_segments:
                item['known_cleaning_areas'] = [segment['name'] for segment in self.vacuum_segments[entity_id]][:16]
                item['known_cleaning_areas_note'] = '这是设备支持的区域，不代表本次实际清扫区域'
            for key in SNAPSHOT_ATTRS:
                if attributes.get(key) is not None:
                    item[key] = attributes.get(key)
            (prioritized if appliance_type in MOVING_APPLIANCE_TYPES else ordinary).append(item)
        primary, repeated, seen_devices = ([], [], set())
        for item in prioritized:
            identity = str(item.get('device') or item.get('name') or item['entity_id'])
            if identity in seen_devices:
                repeated.append(item)
            else:
                seen_devices.add(identity)
                primary.append(item)
        return (primary + repeated + ordinary)[:SNAPSHOT_MAX_ENTITIES]
