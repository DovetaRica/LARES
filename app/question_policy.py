import json
import logging
import re
from pathlib import Path
logger = logging.getLogger(__name__)
POLICY_PATH = (Path(__file__).parent / 'resources' / 'question_policy.json')
DEFAULT_QUOTA_WINDOW = 8
SPARSE_NOTE_LIMIT = 4
_cached = None

class QuestionPolicy:

    def __init__(self, data):
        self.data = data if isinstance(data, dict) else {}
        self.sparse_signals = list(self.data.get('sparse_signals') or [])
        self.forbidden = list(self.data.get('forbidden') or [])
        self.quotas = list(self.data.get('quotas') or [])
        self.preferred_topics = list(self.data.get('preferred_topics') or [])
        self.quota_window = int(self.data.get('quota_window') or DEFAULT_QUOTA_WINDOW)

    @classmethod
    def from_path(cls, path=POLICY_PATH):
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError) as error:
            logger.warning('question policy unavailable (%s): asking without policy', type(error).__name__)
            data = {}
        return cls(data)

    def sparse_notes(self, text, limit=SPARSE_NOTE_LIMIT):
        haystack = str(text or '')
        notes = []
        for entry in self.sparse_signals:
            if not haystack or not self._matches(entry.get('match'), haystack):
                continue
            notes.append({'signal': entry.get('key'), 'reports': entry.get('reports'), 'cannot_claim': entry.get('cannot_claim'), 'note': entry.get('note')})
            if len(notes) >= limit:
                break
        return notes

    @staticmethod
    def _matches(pattern, text):
        if not pattern:
            return False
        try:
            return re.search(pattern, text) is not None
        except re.error:
            return pattern in text

    def forbidden_reason(self, question, why=''):
        text = str(question or '')
        combined = f"{text} {why or ''}"
        for entry in self.forbidden:
            rule = entry.get('rule')
            if rule == 'absence':
                markers = entry.get('absence_markers') or []
                why_markers = entry.get('why_markers') or []
                record_words = entry.get('record_words') or []
                negative = any((marker in combined for marker in markers))
                if not negative:
                    continue
                if any((marker in combined for marker in why_markers)):
                    return entry.get('key')
                if any((word in combined for word in record_words)):
                    return entry.get('key')
            elif rule == 'any_of':
                if any((marker in combined for marker in entry.get('markers') or [])):
                    return entry.get('key')
            elif rule == 'question_shape':
                markers = entry.get('question_markers') or []
                if markers and (not any((marker in text for marker in markers))):
                    return entry.get('key')
        return None

    def quota_reason(self, question, recent_questions):
        text = str(question or '')
        recent = [str(item or '') for item in list(recent_questions or [])[:self.quota_window]]
        for entry in self.quotas:
            pattern = entry.get('match')
            if not self._matches(pattern, text):
                continue
            seen = sum((1 for candidate in recent if self._matches(pattern, candidate)))
            if seen >= int(entry.get('max_in_window') or 1):
                return entry.get('key')
        return None

def default():
    global _cached
    if _cached is None:
        _cached = QuestionPolicy.from_path()
    return _cached

def reset_cache():
    global _cached
    _cached = None
