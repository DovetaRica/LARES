"""Release regression tests for the 0.2.1 additive schema and synthetic spatial config.

Both areas are tested with temporary, synthetic data only: a throwaway SQLite file
built from the historical v0.2.0-alpha.1 SCHEMA literal (read as text, never
executed) and the packaged synthetic example topology. No network, service, secret
or maintainer configuration is used.
"""

import importlib.resources
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.database import Database
from app.home_topology import HomeTopology

REPO_ROOT = Path(__file__).resolve().parents[1]

LEGACY_EVENT = {
    "ts": "2024-01-01T00:00:00+00:00",
    "entity_id": "binary_sensor.example_entry",
    "domain": "binary_sensor",
    "category": "presence",
    "event_type": "state_changed",
    "old_state": "off",
    "new_state": "on",
    "source": "synthetic-regression",
}
LEGACY_QUESTION = "Synthetic release regression question"
LEGACY_CREATED_AT = "2024-01-01T00:00:00+00:00"

BASE = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


def _historical_schema():
    """Read the frozen public v0.2.0-alpha.1 schema, including from source archives."""
    return (REPO_ROOT / "runtime_tests" / "fixtures" / "v020-schema.sql").read_text(encoding="utf-8")


def _write_legacy_database(path):
    schema = _historical_schema()
    with closing(sqlite3.connect(path)) as con:
        con.executescript(schema)
        con.execute(
            "INSERT INTO events (ts, entity_id, domain, category, event_type,"
            " old_state, new_state, source) VALUES (:ts, :entity_id, :domain,"
            " :category, :event_type, :old_state, :new_state, :source)",
            LEGACY_EVENT,
        )
        con.execute(
            "INSERT INTO ai_questions (question, created_at) VALUES (?, ?)",
            (LEGACY_QUESTION, LEGACY_CREATED_AT),
        )
        con.commit()


def _packaged_topology():
    resource = importlib.resources.files("app") / "resources" / "home_topology.json"
    with importlib.resources.as_file(resource) as path:
        return HomeTopology.from_path(path)


def _presence(entity, seconds):
    return {
        "state": "有人",
        "start": (BASE + timedelta(seconds=seconds)).isoformat(),
        "entity": entity,
    }


def test_historical_schema_predates_the_release_additions():
    schema = _historical_schema()
    assert "CREATE TABLE IF NOT EXISTS ai_questions" in schema
    assert "model_calls" not in schema
    assert "options_json" not in schema


def test_legacy_rows_survive_two_current_initializations(tmp_path):
    path = tmp_path / "legacy.db"
    _write_legacy_database(path)

    Database(path)
    database = Database(path)

    assert database.rows("SELECT ts, entity_id, new_state FROM events") == [
        {
            "ts": LEGACY_EVENT["ts"],
            "entity_id": LEGACY_EVENT["entity_id"],
            "new_state": LEGACY_EVENT["new_state"],
        }
    ]
    assert database.scalar("SELECT COUNT(*) FROM ai_questions") == 1
    assert database.scalar("SELECT question FROM ai_questions") == LEGACY_QUESTION


def test_initialization_adds_accounting_schema_and_stays_integral(tmp_path):
    path = tmp_path / "legacy.db"
    _write_legacy_database(path)

    Database(path)
    database = Database(path)

    with database.connect() as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        question_columns = {row[1] for row in con.execute("PRAGMA table_info(ai_questions)")}
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]

    assert "model_calls" in tables
    assert "options_json" in question_columns
    assert integrity == "ok"


def test_packaged_topology_declares_only_the_synthetic_three_node_chain():
    topology = _packaged_topology()
    assert set(topology.nodes) == {"example_entry", "example_hall", "example_room"}
    assert topology.sensors == {
        "binary_sensor.example_entry": "example_entry",
        "binary_sensor.example_hall": "example_hall",
        "binary_sensor.example_room": "example_room",
    }
    assert topology.neighbors == {
        "example_entry": {"example_hall"},
        "example_hall": {"example_entry", "example_room"},
        "example_room": {"example_hall"},
    }


def test_forward_sequence_candidates_stay_adjacent_and_unconfirmed():
    topology = _packaged_topology()
    result = topology.candidates(
        [
            _presence("binary_sensor.example_entry", 0),
            _presence("binary_sensor.example_hall", 10),
            _presence("binary_sensor.example_room", 20),
        ]
    )
    assert [(item["from"], item["to"]) for item in result] == [
        ("Example entry", "Example hall"),
        ("Example hall", "Example room"),
    ]
    assert all(item["unobserved_intermediate"] == [] for item in result)
    assert all("未确认" in item["status"] for item in result)


def test_reverse_sequence_candidates_follow_observation_order():
    topology = _packaged_topology()
    result = topology.candidates(
        [
            _presence("binary_sensor.example_room", 0),
            _presence("binary_sensor.example_hall", 10),
            _presence("binary_sensor.example_entry", 20),
        ]
    )
    assert [(item["from"], item["to"]) for item in result] == [
        ("Example room", "Example hall"),
        ("Example hall", "Example entry"),
    ]
    assert all(item["unobserved_intermediate"] == [] for item in result)
    assert all("未确认" in item["status"] for item in result)


def test_skipping_a_sensed_intermediate_yields_no_candidate():
    topology = _packaged_topology()
    result = topology.candidates(
        [
            _presence("binary_sensor.example_entry", 0),
            _presence("binary_sensor.example_room", 10),
        ]
    )
    assert result == []


def test_gap_beyond_the_packaged_candidate_horizon_yields_no_candidate():
    topology = _packaged_topology()
    result = topology.candidates(
        [
            _presence("binary_sensor.example_entry", 0),
            _presence("binary_sensor.example_hall", topology.max_gap_seconds + 1),
        ]
    )
    assert result == []


def test_context_uses_only_the_supplied_graph():
    topology = HomeTopology({"a": {"label": "Space A"}, "b": {"label": "Space B"}}, [("a", "b")])
    context = topology.context()
    assert context["adjacent_spaces"] == [["Space A", "Space B"]]
    assert "Example entry" not in str(context)
    assert "Example hall" not in str(context)
    assert "未确认" not in context["source"]
    assert "仅来自当前配置" in context["note"]
    assert "不能确认同一人" in context["note"]
