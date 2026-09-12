import argparse
import asyncio
import importlib.util
import json
import sys
import tempfile
from collections import deque
from .database import Database
from .event_aggregator import EventAggregator
from .anomaly_detector import AnomalyDetector
from .memory_manager import MemoryManager
from .pipeline import normalize, timestamp
from pathlib import Path
from . import __version__
from .config import load_config
from .pipeline import Pipeline
from .provider import FixtureProvider, OllamaProvider


def emit(value):
    print(json.dumps(value, ensure_ascii=True))


def replay(path, cfg, provider):
    pipeline = Pipeline(cfg, provider)
    decisions = deque(maxlen=cfg["max_events"])
    decision_count = 0
    with tempfile.TemporaryDirectory(prefix="home-ai-replay-") as folder:
        db = Database(Path(folder) / "replay.db")
        aggregator = EventAggregator(db)
        last_ts = None
        with Path(path).open(encoding="utf-8") as source:
            while True:
                line = source.readline(65537)
                if not line:
                    break
                if len(line) > 65536:
                    raise ValueError("Event line too large")
                if not line.strip():
                    continue
                event = normalize(json.loads(line))
                before = pipeline.accepted
                result = pipeline.feed(event)
                if pipeline.accepted != before:
                    aggregator.process({"ts": event["ts"], "entity_id": event["entity_id"],
                        "category": "environment" if event["kind"] == "environment" else "device",
                        "domain": event["kind"], "event_type": "state_changed", "source": event["source"],
                        "new_state": event["state"], "attributes_json": {}, "metadata_json": {}})
                last_ts = timestamp(event["ts"])
                if result:
                    decision_count += 1
                    decisions.append(result)
        aggregator.flush_environment()
        findings = AnomalyDetector(db).detect(last_ts) if last_ts else []
        candidates = [{"type": "automation_review", "description": "Review repeated manual corrections for " + f["entity_id"],
                       "sample_count": f["sample_count"], "consistency": 0, "evidence": [f],
                       "reason": "Frequency alone does not establish a replacement rule."}
                      for f in findings if f["type"] == "repeated_automation_correction"]
        MemoryManager(db).upsert_candidates(candidates)
        # Only bounded report output; SQLite backing storage is temporary and removed on exit.
        candidate_rows = db.rows("SELECT description,sample_count,status FROM patterns LIMIT ?", (cfg["max_events"],))
    return {"scenario": Path(path).stem, "mode": "shadow", "provider": cfg["provider"],
            "decisions": list(decisions), "decision_count": decision_count,
            "decisions_truncated": decision_count > len(decisions), "review_candidates": candidate_rows,
            "metrics": pipeline.metrics()}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="home-ai")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("demo", "replay", "doctor", "validate-config", "observe"):
        command = sub.add_parser(name)
        command.add_argument("--config")
        if name in ("replay", "observe"):
            command.add_argument("--enable-model", action="store_true")
        if name == "replay":
            command.add_argument("path")
        if name == "demo":
            command.add_argument("--examples", default="examples")
        if name == "doctor":
            command.add_argument("--json", action="store_true")
        if name == "observe":
            command.add_argument("--connect", action="store_true")
            command.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.command == "validate-config":
            emit({"status": "ok", "mode": cfg["mode"]})
        elif args.command == "doctor":
            emit({"status": "ok", "version": __version__, "mode": "shadow", "network_checked": False,
                  "ha_dependency_installed": importlib.util.find_spec("websockets") is not None,
                  "model_configured": bool(cfg["model"]), "config_valid": True})
        elif args.command == "demo":
            cfg["provider"] = "fixture"
            paths = sorted(Path(args.examples).glob("*.jsonl"))
            if not paths:
                raise ValueError("No demo scenarios found")
            for path in paths:
                emit(replay(path, cfg, FixtureProvider()))
        else:
            if cfg["provider"] == "ollama" and not args.enable_model:
                raise ValueError("Model network access requires --enable-model")
            provider = OllamaProvider(cfg) if cfg["provider"] == "ollama" else FixtureProvider()
            if args.command == "replay":
                emit(replay(args.path, cfg, provider))
            else:
                if not args.connect or args.limit < 0:
                    raise ValueError("Observation requires --connect and a nonnegative limit")
                from .observe import observe
                emit(asyncio.run(observe(cfg, provider, args.limit, emit)))
        return 0
    except (Exception,) as exc:
        # Never echo arbitrary exception text, credentials or request contents.
        emit({"status": "error", "error_type": type(exc).__name__, "hint": "Check configuration and docs/deployment.md; no device action was executed."})
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
