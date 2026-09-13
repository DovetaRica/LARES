import asyncio
import contextlib
import io
import json
import socket
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from home_ai.cli import main, replay
from home_ai.config import load_config
from home_ai.pipeline import Pipeline
from home_ai.provider import FixtureProvider, validate_decision
from home_ai.observe import observe, project_event
from home_ai.database import Database
from home_ai.event_aggregator import EventAggregator
from home_ai.memory_manager import MemoryManager


def event(n=1, state="on", source="system", kind="device", entity="example.light", seconds=0):
    return dict(id=str(n), ts=datetime.fromtimestamp(1735689600+seconds,timezone.utc).isoformat(), entity_id=entity,
                state=state, source=source, kind=kind, area="example")


class PublicTests(unittest.TestCase):
    def test_demo_has_no_network(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["demo"]), 0)
        results=[json.loads(x) for x in output.getvalue().splitlines()]
        self.assertEqual(len(results),3)
        self.assertTrue(all(r["metrics"]["executed_actions"] == 0 for r in results))
        corrections=next(r for r in results if r["scenario"] == "automation_correction")
        self.assertEqual(corrections["review_candidates"][0]["sample_count"],3)
        self.assertEqual(corrections["review_candidates"][0]["status"],"candidate")

    def test_routine_events_do_not_invoke_provider(self):
        p=Pipeline(load_config())
        for i in range(200): p.feed(event(i, state=str(i), seconds=i))
        self.assertEqual(p.calls,0)
        self.assertLessEqual(len(p.events),100)
        self.assertLessEqual(len(p.seen),200)

    def test_provider_failure_abstains_and_continues(self):
        class Broken:
            def analyze(self,payload): raise TimeoutError("private endpoint")
        p=Pipeline(load_config(),Broken())
        p.feed(event(source="automation"))
        result=p.feed(event(2,state="off",source="manual",seconds=1))
        self.assertEqual(result["decision"],"abstain")
        self.assertNotIn("private endpoint",json.dumps(result))
        self.assertIsNone(p.feed(event(3,seconds=2)))

    def test_rejects_model_actions_and_fabricated_evidence(self):
        valid=FixtureProvider().analyze({"trigger":"test","events":[{"id":"1"}]})
        for changes in ({"decision":"turn_off"},{"evidence":["invented"]},{"action":"call_service"},{"evidence":[]}):
            with self.assertRaises(ValueError): validate_decision({**valid,**changes},{"1"})

    def test_context_budget_and_age(self):
        class Capture(FixtureProvider):
            payload=None
            def analyze(self,payload):
                self.payload=payload
                return super().analyze(payload)
        cfg={**load_config(),"max_context_chars":1024,"max_age_seconds":20}
        provider=Capture(); p=Pipeline(cfg,provider)
        for i in range(100): p.feed(event(i,seconds=i))
        p.feed(event(100,source="automation",seconds=100))
        p.feed(event(101,state="off",source="manual",seconds=101))
        self.assertLessEqual(len(json.dumps(provider.payload,ensure_ascii=True)),1024)
        self.assertLessEqual(len(p.events),21)

    def test_out_of_order_rejected(self):
        p=Pipeline(load_config()); p.feed(event(seconds=5))
        with self.assertRaises(ValueError): p.feed(event(2,seconds=1))

    def test_duplicates_do_not_invoke(self):
        p=Pipeline(load_config()); p.feed(event(source="automation"))
        raw=event(2,state="off",source="manual",seconds=1)
        p.feed(raw); self.assertIsNone(p.feed(raw)); self.assertEqual(p.calls,1)

    def test_stale_robot_is_not_current_evidence(self):
        p=Pipeline(load_config())
        p.feed(event(kind="robot",entity="example.robot",state="cleaning"))
        self.assertIsNone(p.feed(event(2,kind="presence",entity="example.presence",seconds=200)))

    def test_robot_stopped_is_not_current_evidence(self):
        p=Pipeline(load_config())
        p.feed(event(kind="robot",entity="example.robot",state="cleaning"))
        p.feed(event(2,kind="robot",entity="example.robot",state="idle",seconds=1))
        self.assertIsNone(p.feed(event(3,kind="presence",entity="example.presence",seconds=2)))

    def test_strict_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"config.json"
            for data in ({"mode":"execute"},{"max_events":True},{"model_url":"http://example.invalid"},{"secret":"value"},{"entity_mapping":{"a":"same","b":"same"}}):
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError): load_config(path)

    def test_model_requires_explicit_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"config.json"; path.write_text('{"provider":"ollama","model":"example"}')
            with patch.object(socket.socket,"connect",side_effect=AssertionError),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["replay","home_ai/examples/sensor_conflict.jsonl","--config",str(path)]),2)

    def test_observe_requires_connect(self):
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(main(["observe"]),2)

    def test_projection_drops_private_attributes(self):
        message={"event":{"event_type":"state_changed","time_fired":"2025-01-01T00:00:00Z","data":{
            "entity_id":"light.fixture","new_state":{"state":"on","attributes":{"friendly_name":"PRIVATE_NAME","latitude":99},"context":{"user_id":"PRIVATE_USER"}}}}}
        self.assertIsNone(project_event(message,{}))
        result=project_event(message,{"light.fixture":"example.light"})
        self.assertNotIn("PRIVATE",json.dumps(result))
        self.assertEqual(result["source"],"manual")

    def test_ha_protocol_only_authenticates_and_subscribes(self):
        class FakeSocket:
            def __init__(self):
                self.sent=[]
                self.responses=iter([{"type":"auth_required"},{"type":"auth_ok"},{"success":True}])
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def recv(self): return json.dumps(next(self.responses))
            async def send(self,raw): self.sent.append(json.loads(raw))
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration
        ws=FakeSocket()
        cfg={**load_config(),"ha_url":"https://example.invalid","allow_remote_ha":True,"entity_mapping":{"light.fixture":"example.light"}}
        with patch.dict('sys.modules',{'websockets':types.SimpleNamespace(connect=lambda *a,**k:ws)}), patch.dict('os.environ',{'HOME_AI_HA_TOKEN':'synthetic-test-value'}):
            asyncio.run(observe(cfg,FixtureProvider(),1,lambda x:None))
        self.assertEqual([r['type'] for r in ws.sent],['auth','subscribe_events'])
        self.assertEqual(ws.sent[1]['event_type'],'state_changed')

    def test_extracted_aggregation_is_bounded_and_correct(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Database(Path(folder)/'test.db'); agg=EventAggregator(db)
            for i in range(10000):
                agg.process(dict(ts='2025-01-01T00:00:00Z',entity_id='example.temperature',new_state=str(i),category='environment',source='system',event_type='state_changed'))
            self.assertEqual(len(agg.environment),1)
            self.assertEqual(agg.flush_environment(),1)
            stats=json.loads(db.rows('SELECT metadata_json FROM events')[0]['metadata_json'])
            self.assertEqual(stats['count'],10000); self.assertEqual(stats['avg'],4999.5)

    def test_rejected_candidate_stays_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Database(Path(folder)/'test.db'); memory=MemoryManager(db)
            item=dict(type='review',description='Synthetic candidate',sample_count=3,consistency=.5)
            [pid]=memory.upsert_candidates([item]); memory.feedback(pid,'reject')
            memory.upsert_candidates([{**item,'sample_count':100}])
            self.assertEqual(db.scalar('SELECT status FROM patterns WHERE id=?',(pid,)),'rejected')

if __name__ == '__main__': unittest.main()
