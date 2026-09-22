from .telemetry_stats import finite_number
CONTROL_ATTRIBUTES = {'light': ('brightness', 'color_temp_kelvin', 'hs_color', 'rgb_color', 'xy_color'), 'climate': ('temperature', 'target_temp_low', 'target_temp_high', 'fan_mode', 'preset_mode'), 'fan': ('percentage', 'preset_mode', 'oscillating', 'direction'), 'media_player': ('volume_level', 'is_volume_muted', 'source'), 'humidifier': ('humidity', 'mode'), 'water_heater': ('temperature', 'operation_mode'), 'switch': (), 'cover': (), 'vacuum': (), 'select': (), 'number': (), 'lock': ()}
UNKNOWN_STATES = frozenset({'', 'unknown', 'unavailable', 'none'})

def control_attributes(domain, attributes):
    return {key: attributes[key] for key in CONTROL_ATTRIBUTES.get(domain, ()) if key in attributes and attributes[key] is not None}

def changed_dimensions(domain, before_state, after_state, before_attrs, after_attrs):
    if domain not in CONTROL_ATTRIBUTES:
        return []
    if str(before_state).lower() in UNKNOWN_STATES or str(after_state).lower() in UNKNOWN_STATES:
        return []
    changed = ['state'] if before_state != after_state else []
    for key in CONTROL_ATTRIBUTES[domain]:
        before, after = (before_attrs.get(key), after_attrs.get(key))
        if before is None or after is None or before == after:
            continue
        if any((isinstance(value, (int, float)) and (not isinstance(value, bool)) and (finite_number(value) is None) for value in (before, after))):
            continue
        changed.append(key)
    return changed
