import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
logger = logging.getLogger(__name__)
BACKUP_NAME = 'home_ai-*.db'
BACKUP_KEEP_MIN = 30
BACKUP_KEEP_DAYS = 7

def backup_timestamp(path):
    name = path.name
    stem = name[len('home_ai-'):-len('.db')] if name.startswith('home_ai-') and name.endswith('.db') else ''
    for pattern in ('%Y%m%dT%H%M%SZ', '%Y-%m-%d'):
        try:
            return datetime.strptime(stem, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

def prune_backups(root, keep_min=BACKUP_KEEP_MIN, keep_days=BACKUP_KEEP_DAYS, now=None):
    root = Path(root)
    if not root.is_dir():
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=keep_days)
    files = sorted(((backup_timestamp(path), path) for path in root.glob(BACKUP_NAME)), key=lambda item: item[0], reverse=True)
    removed = []
    for index, (stamp, path) in enumerate(files):
        if index < keep_min or stamp >= cutoff:
            continue
        path.unlink()
        removed.append(path.name)
    _remove_orphan_sidecars(root, removed)
    return removed

def _remove_orphan_sidecars(root, removed):
    for name in removed:
        for suffix in ('-wal', '-shm'):
            sidecar = root / f'{name}{suffix}'
            if sidecar.exists():
                sidecar.unlink()

def create_backup(database, backup_dir, weekly=False):
    root = Path(backup_dir) / ('weekly' if weekly else 'daily')
    stamp = date.today().isoformat()
    destination = root / f'home_ai-{stamp}.db'
    database.backup(destination)
    removed = prune_backups(root)
    if removed:
        logger.info('backup retention removed=%s keep_min=%s keep_days=%s pool=%s', len(removed), BACKUP_KEEP_MIN, BACKUP_KEEP_DAYS, root.name)
    return destination
