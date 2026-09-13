import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlparse

class ConfigError(ValueError):
    """A fixed, safe diagnostic; never include user-supplied values."""

DEFAULTS = {
    "mode": "shadow", "max_events": 100, "max_context_chars": 16000,
    "max_age_seconds": 3600, "cooldown_seconds": 300,
    "queue_capacity": 100, "max_queue_age_seconds": 30,
    "provider": "fixture", "model": "", "model_url": "http://localhost:11434",
    "allow_remote_model": False, "allow_remote_ha": False,
    "ha_url": "", "entity_mapping": {},
}
LIMITS = {"max_events": (1, 100000), "max_context_chars": (1024, 1048576),
          "max_age_seconds": (1, 604800), "cooldown_seconds": (1, 86400),
          "queue_capacity": (1, 10000), "max_queue_age_seconds": (1, 3600)}


def validate_endpoint(value, allow_remote, name, optional=False):
    if type(allow_remote) is not bool:
        raise ConfigError("Remote endpoint switches must be boolean")
    if not isinstance(value, str):
        raise ConfigError("Endpoint URLs must be strings")
    if optional and value == "":
        return None
    try:
        url = urlparse(value)
        if (len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value)
                or url.scheme not in ("http", "https") or not url.hostname
                or url.username is not None or url.password is not None
                or "?" in value or "#" in value or "\\" in value
                or "%" in url.netloc or (url.port is not None and not 1 <= url.port <= 65535)):
            raise ValueError()
        host = url.hostname.lower()
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
                raise ValueError()
            loopback = host == "localhost"
    except (ValueError, TypeError):
        raise ConfigError("Invalid " + name + " URL") from None
    if not loopback:
        if not allow_remote:
            raise ConfigError("Non-loopback " + name + " endpoint requires explicit remote opt-in")
        if url.scheme != "https":
            raise ConfigError("Non-loopback " + name + " endpoint requires HTTPS")
    return url


def validate_config(raw):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ConfigError("Unknown configuration fields")
    cfg = {**DEFAULTS, "entity_mapping": {}, **raw}
    if cfg["mode"] != "shadow":
        raise ConfigError("Only shadow mode is implemented")
    for field, (low, high) in LIMITS.items():
        if type(cfg[field]) is not int or not low <= cfg[field] <= high:
            raise ConfigError("Invalid limit: " + field)
    if cfg["provider"] not in ("fixture", "ollama"):
        raise ConfigError("Unsupported provider")
    if not isinstance(cfg["model"], str) or len(cfg["model"]) > 160:
        raise ConfigError("Model name must be a string of at most 160 characters")
    validate_endpoint(cfg["model_url"], cfg["allow_remote_model"], "model")
    validate_endpoint(cfg["ha_url"], cfg["allow_remote_ha"], "HA", optional=True)
    mapping = cfg["entity_mapping"]
    if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) and 0 < len(k) <= 160 and 0 < len(v) <= 80 for k,v in mapping.items()):
        raise ConfigError("entity_mapping must contain nonempty strings within length limits")
    if len(set(mapping.values())) != len(mapping):
        raise ConfigError("Entity aliases must be unique")
    return cfg


def load_config(path=None):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        raise ConfigError("Cannot read configuration JSON") from None
    return validate_config(raw)
