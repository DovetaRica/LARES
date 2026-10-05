import json
from datetime import datetime
from pathlib import Path
from .llm_schema import compact_json, conservative_token_estimate

class HomeTopology:

    def __init__(self, nodes=None, edges=(), sensors=None, max_gap_seconds=120):
        self.nodes = nodes or {}
        self.edges = [tuple(edge) for edge in edges]
        self.sensors = sensors or {}
        self.max_gap_seconds = max_gap_seconds
        self.neighbors = {node: set() for node in self.nodes}
        for left, right in self.edges:
            self.neighbors[left].add(right)
            self.neighbors[right].add(left)

    @classmethod
    def from_path(cls, path):
        path = Path(path)
        if not path.is_file():
            return cls()
        raw = json.loads(path.read_text(encoding='utf-8'))
        nodes, edges, sensors = (raw['nodes'], raw['edges'], raw['sensors'])
        if raw.get('schema_version') != 1 or not isinstance(nodes, dict) or (not 1 <= len(nodes) <= 24) or (not isinstance(edges, list)) or (len(edges) > 32) or (not isinstance(sensors, dict)) or (len(sensors) > 128):
            raise ValueError('invalid home topology shape')
        if any((not isinstance(node, str) or not isinstance(info, dict) or (not isinstance(info.get('label'), str)) or (not 1 <= len(info['label']) <= 16) for node, info in nodes.items())):
            raise ValueError('invalid home topology node')
        if any((not isinstance(edge, list) or len(edge) != 2 or edge[0] == edge[1] or any((node not in nodes for node in edge)) for edge in edges)):
            raise ValueError('invalid home topology edge')
        if any((not entity.startswith('binary_sensor.') or node not in nodes for entity, node in sensors.items())):
            raise ValueError('invalid home topology sensor')
        gap = raw.get('candidate_max_gap_seconds', 120)
        if type(gap) is not int or not 10 <= gap <= 300:
            raise ValueError('invalid home topology candidate horizon')
        topology = cls(nodes, edges, sensors, gap)
        if conservative_token_estimate(compact_json(topology.context())) > 650:
            raise ValueError('home topology context exceeds its token cap')
        return topology

    def context(self):
        if not self.nodes:
            return {}
        return {'source': '用户配置的空间邻接；附带示例为合成数据', 'adjacent_spaces': [[self.nodes[a]['label'], self.nodes[b]['label']] for a, b in self.edges], 'note': '邻接关系仅来自当前配置，不证明实际移动。缺失中间空间保持未知；顺序候选不能确认同一人、人数、完整路线或到离家。'}

    @staticmethod
    def _time(value):
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return parsed if parsed.tzinfo else None
        except (TypeError, ValueError):
            return None

    def _path(self, source, target):
        if target in self.neighbors[source]:
            return [source, target]
        bridges = (self.neighbors[source] & self.neighbors[target]) - set(self.sensors.values())
        if len(bridges) == 1:
            return [source, next(iter(bridges)), target]
        return None

    def candidates(self, intervals, limit=8):
        starts = []
        for item in intervals:
            if item.get('state') != '有人' or item.get('stream_incomplete'):
                continue
            stamp = self._time(item.get('start'))
            if stamp:
                starts.append((stamp, self.sensors.get(item.get('entity'))))
        starts.sort(key=lambda item: item[0])
        results, previous = ([], None)
        for index, (stamp, node) in enumerate(starts):
            simultaneous = index > 0 and (stamp - starts[index - 1][0]).total_seconds() <= 2 or (index + 1 < len(starts) and (starts[index + 1][0] - stamp).total_seconds() <= 2)
            if node is None or simultaneous:
                previous = None
                continue
            if previous and node != previous[1]:
                gap = (stamp - previous[0]).total_seconds()
                path = self._path(previous[1], node)
                if path and 2 < gap <= self.max_gap_seconds:
                    results.append({'from': self.nodes[previous[1]]['label'], 'to': self.nodes[node]['label'], 'time': stamp.isoformat(), 'gap_seconds': round(gap), 'unobserved_intermediate': [self.nodes[n]['label'] for n in path[1:-1]], 'status': '可能的移动方向，未确认同一人'})
            previous = (stamp, node)
        return results[-limit:]
