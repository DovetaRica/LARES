import json
from pathlib import Path
from urllib.parse import urlparse

DEFAULTS = {
    "mode": "shadow", "max_events": 100, "max_context_chars": 16000,
    "max_age_seconds": 3600, "cooldown_seconds": 300,
    "provider": "fixture", "model": "", "model_url": "http://localhost:11434",
    "allow_remote_model": False, "ha_url": "", "entity_mapping": {},
}

def load_config(path=None):
    cfg = DEFAULTS.copy()
    if path:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
            raise ValueError("Unknown configuration fields")
        cfg.update(raw)
    if cfg["mode"] != "shadow":
        raise ValueError("Only shadow mode is implemented")
    for field in ("max_events", "max_context_chars", "max_age_seconds", "cooldown_seconds"):
        if type(cfg[field]) is not int or cfg[field] <= 0:
            raise ValueError("Limits must be positive integers")
    if cfg["max_context_chars"] < 1024:
        raise ValueError("max_context_chars must be at least 1024")
    if cfg["provider"] not in ("fixture", "ollama"):
        raise ValueError("Unsupported provider")
    if type(cfg["allow_remote_model"]) is not bool:
        raise ValueError("allow_remote_model must be boolean")
    url = urlparse(cfg["model_url"])
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Invalid model URL")
    if url.hostname not in ("localhost", "127.0.0.1", "::1") and not cfg["allow_remote_model"]:
        raise ValueError("Non-loopback model endpoint requires allow_remote_model")
    mapping = cfg["entity_mapping"]
    if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) and k and v for k,v in mapping.items()):
        raise ValueError("entity_mapping must contain nonempty strings")
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("Entity aliases must be unique")
    return cfg
