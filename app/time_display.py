from datetime import datetime, timezone
from zoneinfo import ZoneInfo
BEIJING = ZoneInfo('Asia/Shanghai')

def chinese_label(value, seconds=True, tz=None):
    text = str(value or '')
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(tz or BEIJING)
    stamp = f'{local.year}年{local.month}月{local.day}日'
    if 'T' not in text and ' ' not in text:
        return stamp
    return f'{stamp} {local:%H:%M:%S}' if seconds else f'{stamp} {local:%H:%M}'

def moment(value, tz=None):
    return chinese_label(value, seconds=True, tz=tz) or value
