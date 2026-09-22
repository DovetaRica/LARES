import bisect
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import yaml
from .robot_presence import ACTIVE_STATES as ROBOT_ACTIVE_STATES
ALGORITHM_VERSION = 'cooccurrence-v1'
CLEAN_TAIL_HOURS = 72

@dataclass(frozen=True)
class CooccurrenceConfig:
    algorithm_version: int = 1
    actor_types: tuple[str, ...] = ('vacuum', 'washing_machine', 'dryer')
    active_states: tuple[str, ...] = ('cleaning', 'start')
    lookback_hours: int = 336
    max_episode_minutes: int = 120
    min_episode_seconds: int = 60
    control_windows_per_episode: int = 3
    min_control_windows: int = 5
    min_episodes: int = 10
    min_lift: float = 2.0
    min_span_days: int = 3
    max_pairs: int = 20
    model_max_pairs: int = 5
    max_findings_per_actor: int = 3
    max_actor_entities: int = 50
    max_sensor_entities: int = 50

    @classmethod
    def from_mapping(cls, mapping):
        if mapping is None:
            return cls()
        if not isinstance(mapping, dict):
            raise ValueError('cooccurrence must be a mapping')
        allowed = set(cls.__dataclass_fields__)
        unknown = set(mapping) - allowed
        if unknown:
            raise ValueError(f'cooccurrence unknown field: {sorted(unknown)[0]}')
        values = dict(mapping)
        for field in ('actor_types', 'active_states'):
            raw = values.get(field, getattr(cls(), field))
            if not isinstance(raw, (list, tuple)) or not raw or any((not isinstance(item, str) or not item.strip() for item in raw)):
                raise ValueError(f'cooccurrence invalid field: {field}')
            values[field] = tuple(dict.fromkeys((item.strip().lower() for item in raw)))
        integer_limits = {'algorithm_version': (1, 1), 'lookback_hours': (24, 2160), 'max_episode_minutes': (1, 1440), 'min_episode_seconds': (1, 86400), 'control_windows_per_episode': (1, 10), 'min_control_windows': (1, 1000), 'min_episodes': (1, 1000), 'min_span_days': (1, 365), 'max_pairs': (1, 100), 'model_max_pairs': (1, 100), 'max_findings_per_actor': (1, 20), 'max_actor_entities': (1, 500), 'max_sensor_entities': (1, 500)}
        defaults = cls()
        for field, (lower, upper) in integer_limits.items():
            raw = values.get(field, getattr(defaults, field))
            if isinstance(raw, bool) or not isinstance(raw, int) or (not lower <= raw <= upper):
                raise ValueError(f'cooccurrence invalid field: {field}')
            values[field] = raw
        raw_lift = values.get('min_lift', defaults.min_lift)
        if isinstance(raw_lift, bool):
            raise ValueError('cooccurrence invalid field: min_lift')
        try:
            raw_lift = float(raw_lift)
        except (TypeError, ValueError) as error:
            raise ValueError('cooccurrence invalid field: min_lift') from error
        if not math.isfinite(raw_lift) or not 1.0 <= raw_lift <= 1000.0:
            raise ValueError('cooccurrence invalid field: min_lift')
        values['min_lift'] = raw_lift
        config = cls(**values)
        if config.model_max_pairs > config.max_pairs:
            raise ValueError('cooccurrence invalid field: model_max_pairs')
        return config

    def canonical_mapping(self):
        result = asdict(self)
        result['actor_types'] = list(self.actor_types)
        result['active_states'] = list(self.active_states)
        return result

    @property
    def semantic_hash(self):
        raw = json.dumps(self.canonical_mapping(), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]

@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

class CooccurrenceAnalyzer:

    def __init__(self, database, config, timezone_name='Asia/Shanghai', gated_presence_entities=()):
        self.db = database
        self.config = config if isinstance(config, CooccurrenceConfig) else CooccurrenceConfig.from_mapping(config)
        self.tz = ZoneInfo(timezone_name)
        self.gated_presence_entities = frozenset(gated_presence_entities or ())

    @classmethod
    def from_path(cls, database, config_path, timezone_name='Asia/Shanghai', gated_presence_entities=()):
        path = Path(config_path)
        root = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
        if root is not None and (not isinstance(root, dict)):
            raise ValueError('entities config must be a mapping')
        return cls(database, (root or {}).get('cooccurrence'), timezone_name, gated_presence_entities=gated_presence_entities)

    @staticmethod
    def _parse_ts(value):
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('event timestamp has no timezone')
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _overlaps(left, right):
        return left.start < right.end and right.start < left.end

    @staticmethod
    def _day_type(value):
        return 'weekday' if value.weekday() < 5 else 'weekend'

    @staticmethod
    def _deduplicate(rows):
        seen, result = (set(), [])
        for row in rows:
            key = (row.get('entity_id'), row.get('ts'), row.get('old_state'), row.get('new_state'))
            if key not in seen:
                seen.add(key)
                result.append(row)
        return result

    def _query_rows(self, start_utc, end_utc, domain, category, include_metadata=False):
        margin = timedelta(hours=24)
        rough_start = (start_utc - margin).isoformat()
        rough_end = (end_utc + margin).isoformat()
        metadata_column = ',metadata_json' if include_metadata else ''
        rows = self.db.rows(f'SELECT id,ts,entity_id,old_state,new_state,area{metadata_column} FROM events WHERE ts>=? AND ts<? AND domain=? AND category=? ORDER BY datetime(ts),id', (rough_start, rough_end, domain, category))
        seen, result = (set(), [])
        for row in rows:
            key = (row.get('entity_id'), row.get('ts'), row.get('old_state'), row.get('new_state'))
            if key in seen:
                continue
            seen.add(key)
            try:
                row['parsed_ts'] = self._parse_ts(row['ts'])
            except (TypeError, ValueError):
                continue
            if start_utc - margin <= row['parsed_ts'] < end_utc:
                result.append(row)
        return result

    @staticmethod
    def explain_queries(database):
        plans = {}
        for name, domain, category in (('actors_vacuum', 'vacuum', 'appliance'), ('actors_sensor', 'sensor', 'appliance'), ('sensors', 'binary_sensor', 'presence')):
            plans[name] = database.rows('EXPLAIN QUERY PLAN SELECT id FROM events WHERE ts>=? AND ts<? AND domain=? AND category=? ORDER BY datetime(ts),id', ('2000-01-01T00:00:00+00:00', '2100-01-01T00:00:00+00:00', domain, category))
        return plans

    def _select_entities(self, rows, maximum):
        latest = {}
        for row in rows:
            entity_id = row.get('entity_id')
            if entity_id:
                latest[entity_id] = max(latest.get(entity_id, row['parsed_ts']), row['parsed_ts'])
        ordered = sorted(latest, key=lambda entity_id: (-latest[entity_id].timestamp(), entity_id))
        return (ordered[:maximum], max(0, len(ordered) - maximum), len(ordered))

    def _is_configured_actor(self, row):
        try:
            metadata = json.loads(row.get('metadata_json') or '{}')
        except (TypeError, json.JSONDecodeError):
            return False
        return isinstance(metadata, dict) and metadata.get('appliance_type') in self.config.actor_types

    @staticmethod
    def _appliance_type(row):
        try:
            metadata = json.loads(row.get('metadata_json') or '{}')
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(metadata, dict):
            return None
        return metadata.get('appliance_type')

    def _active_states_for(self, row):
        appliance_type = self._appliance_type(row)
        known = ROBOT_ACTIVE_STATES.get(appliance_type) if appliance_type else None
        if known:
            return known
        return set(self.config.active_states)

    def _episodes(self, actor_rows, actor_ids, start_utc, end_utc):
        grouped = {entity_id: [] for entity_id in actor_ids}
        for row in actor_rows:
            if row.get('entity_id') not in grouped:
                continue
            if not self._is_configured_actor(row):
                continue
            grouped[row['entity_id']].append(row)
        valid, excluded = ([], {'short': 0, 'truncated': 0, 'left_truncated': 0, 'right_truncated': 0})
        for entity_id in actor_ids:
            opened = None
            for row in grouped[entity_id]:
                active_states = self._active_states_for(row)
                new_state = str(row.get('new_state') or '').strip().lower()
                old_state = str(row.get('old_state') or '').strip().lower()
                is_active = new_state in active_states
                was_active = old_state in active_states
                if is_active and opened is None and (not was_active):
                    opened = row['parsed_ts']
                elif not is_active and opened is not None:
                    window = Window(opened, row['parsed_ts'])
                    opened = None
                    if window.start < start_utc:
                        excluded['left_truncated'] += 1
                        continue
                    if window.end > end_utc:
                        excluded['right_truncated'] += 1
                        continue
                    seconds = (window.end - window.start).total_seconds()
                    if seconds < self.config.min_episode_seconds:
                        excluded['short'] += 1
                    elif seconds > self.config.max_episode_minutes * 60:
                        excluded['truncated'] += 1
                    else:
                        valid.append((entity_id, window))
            if opened is not None:
                excluded['left_truncated' if opened < start_utc else 'right_truncated'] += 1
        valid.sort(key=lambda item: (item[0], item[1].start, item[1].end))
        return (valid, excluded)

    def _controls(self, episodes, start_local, end_local):
        all_windows = [window for _, window in episodes]
        by_actor = {}
        for actor, window in episodes:
            by_actor.setdefault(actor, []).append(window)
        dates = []
        cursor = start_local.date()
        while cursor <= end_local.date():
            dates.append(cursor)
            cursor += timedelta(days=1)
        result = {}
        for actor in sorted(by_actor):
            chosen, chosen_starts = ([], set())
            for episode in by_actor[actor]:
                local_start = episode.start.astimezone(self.tz)
                candidates = []
                for candidate_date in dates:
                    if self._day_type(datetime.combine(candidate_date, time.min)) != self._day_type(local_start):
                        continue
                    candidate_local = datetime(candidate_date.year, candidate_date.month, candidate_date.day, local_start.hour, local_start.minute, local_start.second, local_start.microsecond, tzinfo=self.tz)
                    candidate = Window(candidate_local.astimezone(timezone.utc), candidate_local.astimezone(timezone.utc) + (episode.end - episode.start))
                    if candidate.start < start_local.astimezone(timezone.utc) or candidate.end > end_local.astimezone(timezone.utc):
                        continue
                    if candidate.start in chosen_starts or any((self._overlaps(candidate, item) for item in all_windows)):
                        continue
                    candidates.append(candidate)
                candidates.sort(key=lambda item: (abs((item.start - episode.start).total_seconds()), item.start))
                for candidate in candidates[:self.config.control_windows_per_episode]:
                    if candidate.start not in chosen_starts:
                        chosen.append(candidate)
                        chosen_starts.add(candidate.start)
            result[actor] = sorted(chosen, key=lambda item: item.start)
        return result

    def _edges(self, sensor_rows, sensor_ids, start_utc, end_utc):
        result = {entity_id: [] for entity_id in sensor_ids}
        for row in sensor_rows:
            if row.get('entity_id') not in result or not start_utc <= row['parsed_ts'] < end_utc:
                continue
            old_state = str(row.get('old_state') or '').strip().lower()
            new_state = str(row.get('new_state') or '').strip().lower()
            if old_state == 'off' and new_state == 'on':
                result[row['entity_id']].append(row['parsed_ts'])
        for values in result.values():
            values.sort()
        return result

    @staticmethod
    def _window_has_edge(edge_times, window):
        index = bisect.bisect_left(edge_times, window.start)
        return index < len(edge_times) and edge_times[index] < window.end

    def _finding(self, actor, sensor, episodes, controls, edge_times, start_local, end_local):
        episodes_with = [window for window in episodes if self._window_has_edge(edge_times, window)]
        if not episodes or not episodes_with:
            return (None, 'rate_in_zero')
        if len(controls) < self.config.min_control_windows:
            return (None, 'controls_insufficient')
        controls_with = [window for window in controls if self._window_has_edge(edge_times, window)]
        rate_in_raw = len(episodes_with) / len(episodes)
        rate_out_raw = len(controls_with) / len(controls)
        episode_days = len({window.start.astimezone(self.tz).date() for window in episodes})
        inside_edges = []
        for window in episodes_with:
            left = bisect.bisect_left(edge_times, window.start)
            right = bisect.bisect_left(edge_times, window.end)
            inside_edges.extend(edge_times[left:right])
        edge_days = len({value.astimezone(self.tz).date() for value in inside_edges})
        baseline_zero = rate_out_raw == 0
        if baseline_zero:
            if len(episodes) < 2 * self.config.min_episodes or episode_days < 2 * self.config.min_span_days:
                return (None, 'baseline_zero_threshold')
            lift_raw, status = (None, 'needs_observation')
        else:
            lift_raw = rate_in_raw / rate_out_raw
            if len(episodes) < self.config.min_episodes or lift_raw < self.config.min_lift or episode_days < self.config.min_span_days:
                return (None, 'reported_threshold')
            status = 'reported'
        finding_id = hashlib.sha256(f'{ALGORITHM_VERSION}|{actor}|{sensor}'.encode()).hexdigest()[:24]
        finding = {'kind': 'cooccurrence', 'algorithm_version': ALGORITHM_VERSION, 'finding_id': finding_id, 'actor_entity_id': actor, 'sensor_entity_id': sensor, 'episodes_total': len(episodes), 'episodes_with_edge': len(episodes_with), 'control_windows_total': len(controls), 'control_windows_with_edge': len(controls_with), 'rate_in': round(rate_in_raw, 4), 'rate_out': round(rate_out_raw, 4), 'lift': None if lift_raw is None else round(lift_raw, 4), 'baseline_zero': baseline_zero, 'episode_local_days': episode_days, 'edge_local_days': edge_days, 'first_seen': min(inside_edges).astimezone(self.tz).isoformat(), 'last_seen': max(inside_edges).astimezone(self.tz).isoformat(), 'lookback_start': start_local.isoformat(), 'lookback_end': end_local.isoformat(), 'status': status, 'description': f"{actor} 与 {sensor} 在清扫期间呈统计共现：{len(episodes_with)}/{len(episodes)} 个 episode 命中，lift={('基线为零' if lift_raw is None else f'{lift_raw:.4f}')}；这不是因果结论。", '_sort_lift': lift_raw}
        return (finding, None)

    @staticmethod
    def _compact(finding):
        keys = ('kind', 'algorithm_version', 'finding_id', 'actor_entity_id', 'sensor_entity_id', 'episodes_total', 'episodes_with_edge', 'control_windows_total', 'control_windows_with_edge', 'rate_in', 'rate_out', 'lift', 'baseline_zero', 'episode_local_days', 'edge_local_days', 'status')
        return {key: finding[key] for key in keys}

    @staticmethod
    def _recover_incomplete_sensor_rows(sensor_rows, end_utc):
        by_entity = {}
        for row in sensor_rows:
            by_entity.setdefault(row.get('entity_id'), []).append(row)
        kept, dropped, recovered = ([], 0, 0)
        for rows in by_entity.values():
            rows.sort(key=lambda row: (row['parsed_ts'], row.get('id') or 0))
            previous = None
            last_bad = None
            for index, row in enumerate(rows):
                old_state = row.get('old_state')
                if old_state is not None and previous is not None and (str(old_state) != previous):
                    last_bad = index
                new_state = row.get('new_state')
                if new_state is not None:
                    previous = str(new_state)
            if last_bad is None:
                kept.extend(rows)
                continue
            tail = rows[last_bad:]
            clean_age = end_utc - tail[0]['parsed_ts']
            states = {str(row.get('new_state') or '').lower() for row in tail}
            if clean_age >= timedelta(hours=CLEAN_TAIL_HOURS) and {'on', 'off'}.issubset(states):
                kept.extend(tail)
                recovered += 1
            else:
                dropped += 1
        kept.sort(key=lambda row: (row['parsed_ts'], row.get('id') or 0))
        return (kept, dropped, recovered)

    def analyze(self, report_end):
        if report_end.tzinfo is None:
            raise ValueError('report_end must be timezone-aware')
        if report_end.utcoffset() != timedelta(hours=8):
            raise ValueError('report_end must use Beijing time')
        end_local = report_end.astimezone(self.tz)
        start_local = end_local - timedelta(hours=self.config.lookback_hours)
        start_utc, end_utc = (start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc))
        actor_rows = []
        for actor_domain in ('vacuum', 'sensor', 'binary_sensor', 'switch'):
            actor_rows.extend(self._query_rows(start_utc, end_utc, actor_domain, 'appliance', include_metadata=True))
        sensor_rows = self._query_rows(start_utc, end_utc, 'binary_sensor', 'presence')
        if self.gated_presence_entities:
            sensor_rows = [row for row in sensor_rows if row.get('entity_id') not in self.gated_presence_entities]
        incomplete_dropped = 0
        incomplete_recovered = 0
        if sensor_rows:
            sensor_rows, incomplete_dropped, incomplete_recovered = self._recover_incomplete_sensor_rows(sensor_rows, end_utc)
        exact_actor_rows = [row for row in actor_rows if start_utc <= row['parsed_ts'] < end_utc and self._is_configured_actor(row)]
        actor_ids, actor_omitted, actor_total = self._select_entities(exact_actor_rows, self.config.max_actor_entities)
        exact_sensor_rows = [row for row in sensor_rows if start_utc <= row['parsed_ts'] < end_utc]
        sensor_ids, sensor_omitted, sensor_total = self._select_entities(exact_sensor_rows, self.config.max_sensor_entities)
        sensor_area = {}
        for row in exact_sensor_rows:
            area = row.get('area')
            entity = row.get('entity_id')
            if area and entity and (entity not in sensor_area):
                sensor_area[entity] = str(area)
        episodes, excluded = self._episodes(actor_rows, actor_ids, start_utc, end_utc)
        by_actor = {actor: [] for actor in actor_ids}
        for actor, window in episodes:
            by_actor.setdefault(actor, []).append(window)
        controls = self._controls(episodes, start_local, end_local)
        edges = self._edges(sensor_rows, sensor_ids, start_utc, end_utc)
        findings, skipped = ([], {})
        for actor in actor_ids:
            for sensor in sensor_ids:
                if actor == sensor:
                    continue
                finding, reason = self._finding(actor, sensor, by_actor.get(actor, []), controls.get(actor, []), edges.get(sensor, []), start_local, end_local)
                if finding:
                    findings.append(finding)
                elif reason:
                    skipped[reason] = skipped.get(reason, 0) + 1
        findings.sort(key=lambda item: (item['status'] != 'reported', -(item['_sort_lift'] if item['_sort_lift'] is not None else item['episodes_total']), item['finding_id']))
        diversified, seen_area, per_actor = ([], set(), {})
        for item in findings:
            actor = item['actor_entity_id']
            sensor = item['sensor_entity_id']
            area = sensor_area.get(sensor)
            group = (actor, area if area else sensor)
            if group in seen_area:
                skipped['same_area'] = skipped.get('same_area', 0) + 1
                continue
            if per_actor.get(actor, 0) >= self.config.max_findings_per_actor:
                skipped['per_actor_cap'] = skipped.get('per_actor_cap', 0) + 1
                continue
            seen_area.add(group)
            per_actor[actor] = per_actor.get(actor, 0) + 1
            diversified.append(item)
        findings = diversified
        full = []
        for item in findings[:self.config.max_pairs]:
            clean = dict(item)
            clean.pop('_sort_lift', None)
            full.append(clean)
        compact = [self._compact(item) for item in full[:self.config.model_max_pairs]]
        meta = {'algorithm_version': ALGORITHM_VERSION, 'config_hash': self.config.semantic_hash, 'lookback_start': start_local.isoformat(), 'lookback_end': end_local.isoformat(), 'actor_candidates': actor_total, 'actors_selected': len(actor_ids), 'actors_omitted': actor_omitted, 'sensor_candidates': sensor_total, 'sensors_selected': len(sensor_ids), 'sensors_omitted': sensor_omitted, 'sensors_incomplete_dropped': incomplete_dropped, 'sensors_incomplete_recovered': incomplete_recovered, 'episodes_valid': len(episodes), 'episodes_excluded': excluded, 'control_windows': sum((len(items) for items in controls.values())), 'findings_before_limit': len(findings), 'findings_full': len(full), 'findings_compact': len(compact), 'findings_omitted_full': max(0, len(findings) - len(full)), 'findings_omitted_compact': max(0, len(full) - len(compact)), 'max_findings_per_actor': self.config.max_findings_per_actor, 'skipped_pairs': skipped}
        return {'compact': compact, 'full': full, 'meta': meta}
