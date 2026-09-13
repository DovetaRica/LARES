import asyncio
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from home_ai.cli import main, replay
from home_ai.config import ConfigError, load_config, validate_config
from home_ai.database import Database
from home_ai.event_aggregator import EventAggregator
from home_ai.observe import observe
from home_ai.pipeline import Pipeline
from home_ai.provider import FixtureProvider, OllamaProvider, NoRedirect, bounded_payload, serialize_request
from scripts.check_public import scan_artifact, scan_text, scan_repository
from test_public import event


def message(index=0, state="on", source="system"):
    return json.dumps({"event":{"event_type":"state_changed","time_fired":event(seconds=index)["ts"],
        "data":{"entity_id":"light.fixture","new_state":{"state":state,"attributes":{},
            "context":{"id":str(index),"user_id":"synthetic-user" if source == "manual" else None,
                       "parent_id":"synthetic-parent" if source == "automation" else None}}}}})

class FakeSocket:
    def __init__(self, messages):
        self.messages=iter(messages); self.sent=[]
        self.responses=iter([{"type":"auth_required"},{"type":"auth_ok"},{"success":True}])
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass
    async def recv(self): return json.dumps(next(self.responses))
    async def send(self,raw): self.sent.append(json.loads(raw))
    def __aiter__(self): return self
    async def __anext__(self):
        try: return next(self.messages)
        except StopIteration: raise StopAsyncIteration


def observe_config(**overrides):
    return validate_config({"ha_url":"http://localhost:8123","entity_mapping":{"light.fixture":"example.light"},**overrides})


def mocked_observe(ws, cfg=None):
    return patch.dict(sys.modules, {"websockets":types.SimpleNamespace(connect=lambda *args,**kwargs:ws)})

class ReviewFixTests(unittest.TestCase):
    def test_endpoints_share_remote_and_tls_policy(self):
        for name in ("ha", "model"):
            for url in ("http://example.invalid", "https://example.invalid"):
                with self.assertRaises(ConfigError): validate_config({name+"_url":url})
            with self.assertRaises(ConfigError):
                validate_config({name+"_url":"http://example.invalid","allow_remote_"+name:True})
            self.assertEqual(validate_config({name+"_url":"https://example.invalid","allow_remote_"+name:True})[name+"_url"],"https://example.invalid")
            for url in ("http://127.0.0.1:8123","http://[::1]:8123","http://localhost:8123"):
                validate_config({name+"_url":url})

    def test_bad_ha_configuration_fails_before_network(self):
        for value in (False, None, [], "https://user:pass@example.invalid", "http://localhost:abc", "http://localhost:0", "http://localhost#", "http://localhost?", "http://local host", "file:///tmp/x"):
            with self.subTest(value=value), self.assertRaises(ConfigError): validate_config({"ha_url":value})
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as out:
            p=Path(folder)/"config.json"; p.write_text(json.dumps({"ha_url":"https://user:PRIVATE_VALUE@example.invalid"}))
            self.assertEqual(main(["validate-config","--config",str(p)]),2)
            self.assertNotIn("PRIVATE_VALUE",out.getvalue())
            self.assertIn("Invalid HA URL",out.getvalue())

    def test_direct_observe_cannot_bypass_gate(self):
        with patch.dict(os.environ,{"HOME_AI_HA_TOKEN":"synthetic-test-value"}), patch.dict(sys.modules,{"websockets":types.SimpleNamespace(connect=lambda *a,**k: self.fail("Unexpected HA connection"))}):
            with self.assertRaises(ConfigError): asyncio.run(observe({**load_config(),"ha_url":"http://example.invalid"},FixtureProvider(),1,lambda _:None))

    def test_malformed_messages_continue_and_count(self):
        broken=json.loads(message()); del broken["event"]["time_fired"]
        ws=FakeSocket(["not-json","null","[]",json.dumps(broken),message(1)])
        with mocked_observe(ws),patch.dict(os.environ,{"HOME_AI_HA_TOKEN":"synthetic-test-value"}):
            metrics=asyncio.run(observe(observe_config(),FixtureProvider(),1,lambda _:None))
        self.assertEqual(metrics["skipped_malformed"],4)
        self.assertEqual(metrics["processed_messages"],1)
        exc=RuntimeError("redirect"); self.assertIs(ws.process_redirect(exc),exc)
        self.assertEqual([m["type"] for m in ws.sent],["auth","subscribe_events"])

    def test_slow_provider_does_not_block_ingestion_and_queue_is_bounded(self):
        started=threading.Event(); release=threading.Event()
        class Slow(FixtureProvider):
            def analyze(self,payload):
                started.set()
                if not release.wait(2): raise AssertionError("Ingestion did not finish while model was running")
                return super().analyze(payload)
        class Burst(FakeSocket):
            received=0
            async def __anext__(self):
                await asyncio.sleep(0)
                if self.received == 2:
                    if not await asyncio.to_thread(started.wait,1): raise AssertionError("Inference did not start")
                try: item=await super().__anext__()
                except StopAsyncIteration:
                    release.set(); raise
                self.received+=1
                return item
        ws=Burst([message(0,source="automation"),message(1,"off","manual")]+[message(i,str(i)) for i in range(2,22)])
        try:
            with mocked_observe(ws),patch.dict(os.environ,{"HOME_AI_HA_TOKEN":"synthetic-test-value"}):
                metrics=asyncio.run(observe(observe_config(queue_capacity=2),Slow(),0,lambda _:None))
        finally: release.set()
        self.assertEqual(ws.received,22)
        self.assertGreater(metrics["queue_dropped"],0)
        self.assertLessEqual(metrics["queue_peak"],2)
        self.assertEqual(metrics["provider_failures"],0)

    def test_expired_queue_items_are_counted(self):
        clock=[0]
        class Expiring(FakeSocket):
            async def __anext__(self):
                try: return await super().__anext__()
                except StopAsyncIteration:
                    clock[0]=10; raise
        ws=Expiring([message()])
        with mocked_observe(ws),patch.dict(os.environ,{"HOME_AI_HA_TOKEN":"synthetic-test-value"}),patch('home_ai.observe.time',types.SimpleNamespace(monotonic=lambda:clock[0])):
            result=asyncio.run(observe(observe_config(max_queue_age_seconds=1),FixtureProvider(),0,lambda _:None))
        self.assertEqual(result["queue_expired"],1)
        self.assertEqual(result["processed_messages"],0)

    def test_full_request_budget_matches_linear_selection_with_escaping(self):
        cfg={**load_config(),"max_context_chars":1500}
        events=[event(i,entity='example.'+'\"\\\n'+chr(0x4e2d),seconds=i) for i in range(10)]
        actual=bounded_payload("test",events,cfg)
        expected={"trigger":"test","events":events.copy()}
        while len(serialize_request(expected,cfg))>cfg["max_context_chars"] and expected["events"]:
            expected["events"].pop(0)
        self.assertEqual(actual,expected)
        self.assertLessEqual(len(serialize_request(actual,cfg)),1500)
        self.assertEqual(actual["events"][-1]["id"],"9")

    def test_provider_http_request_and_budget_without_network(self):
        captured={}
        good={"decision":"abstain","reason_code":"test","evidence":[],"explanation":"test","uncertainty":"test"}
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,n):
                captured["read_limit"]=n
                return json.dumps({"message":{"content":json.dumps(good)}}).encode()
        def open_request(req,timeout):
            captured["request"]=req; captured["timeout"]=timeout; return Response()
        provider=OllamaProvider({**load_config(),"model":"synthetic-model"})
        with patch('urllib.request.build_opener',return_value=types.SimpleNamespace(open=open_request)) as build:
            self.assertEqual(provider.analyze({"trigger":"test","events":[]}),good)
        self.assertEqual(captured["timeout"],30)
        self.assertEqual(captured["read_limit"],65537)
        self.assertEqual(build.call_args.args[0].proxies,{})
        self.assertIsNone(NoRedirect().redirect_request(None,None,None,None,None,None))
        request=json.loads(captured["request"].data)
        self.assertEqual(request["options"]["num_predict"],512)
        with patch('urllib.request.build_opener',side_effect=AssertionError("No oversized request")):
            with self.assertRaises(ValueError): provider.analyze({"trigger":"test","events":["x"*20000]})

    def test_budget_cooldown_metrics_and_expiry(self):
        cfg={**load_config(),"max_events":2,"cooldown_seconds":10}
        p=Pipeline(cfg)
        for i in range(3):
            p.feed(event(i*2,entity=f'example.x{i}',source="automation",seconds=i*2))
            p.feed(event(i*2+1,entity=f'example.x{i}',state="off",source="manual",seconds=i*2+1))
        self.assertEqual(p.metrics()["budget_skipped"],1)
        p.feed(event(8,entity='example.x2',source='automation',seconds=20))
        p.feed(event(9,entity='example.x2',state='off',source='manual',seconds=21))
        self.assertEqual(p.calls,3)
        p.feed(event(10,entity='example.x2',source='automation',seconds=22))
        p.feed(event(11,entity='example.x2',state='off',source='manual',seconds=23))
        self.assertEqual(p.metrics()["cooldown_skipped"],1)

    def test_batch_atomicity_reuse_backup_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Database(Path(folder)/'test.db')
            try:
                with db.batch():
                    db.set_state('sample','value')
                    with self.assertRaises(RuntimeError):
                        with db.batch(): pass
                    with self.assertRaises(RuntimeError): db.backup(Path(folder)/'forbidden.db')
                    raise RuntimeError('rollback')
            except RuntimeError: pass
            self.assertEqual(db.scalar('SELECT COUNT(*) FROM system_state'),0)
            with patch('home_ai.database.sqlite3.connect',wraps=__import__('sqlite3').connect) as connect:
                with db.batch():
                    for i in range(200): db.set_state(str(i),i)
                self.assertEqual(connect.call_count,1)
            db.backup(Path(folder)/'backup.db')
            self.assertEqual(Database(Path(folder)/'backup.db').scalar('SELECT COUNT(*) FROM system_state'),200)
            self.assertEqual(db.scalar('PRAGMA foreign_keys'),1)

    def test_environment_eviction_flushes_only_oldest_entity(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Database(Path(folder)/'test.db'); agg=EventAggregator(db,max_entities=2)
            for i in range(3): agg.process(dict(ts='2025-01-01T00:00:00Z',entity_id=str(i),new_state='1',category='environment',source='system',event_type='state_changed'))
            self.assertEqual(len(agg.environment),2)
            self.assertEqual(db.scalar('SELECT COUNT(*) FROM events'),1)
            agg.flush_environment()
            self.assertEqual(db.scalar('SELECT COUNT(*) FROM events'),3)

    def test_packaged_demo_runs_from_unrelated_directory(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            env={**os.environ,'PYTHONPATH':str(root),'PYTHONDONTWRITEBYTECODE':'1'}
            result=subprocess.run([sys.executable,'-m','home_ai','demo'],cwd=folder,env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(len(result.stdout.splitlines()),3)

    def test_extended_scanner_and_artifacts(self):
        values=['AK'+'IA'+'A'*16,'sk'+'-'+'a'*24,'xo'+'xb-'+'12345678901','AI'+'za'+'a'*32,
                'password'+'="'+'secretvalue123'+'"','token'+'=secretvalue123','172'+'.20.1.2']
        for value in values:
            self.assertTrue(scan_text(value),value[:3])
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder)/'test.zip'
            with zipfile.ZipFile(archive,'w') as z: z.writestr('.env','DEMO=synthetic')
            self.assertTrue(scan_artifact(archive))
            with zipfile.ZipFile(archive,'w') as z: z.writestr('readme.txt',values[0])
            self.assertTrue(scan_artifact(archive))
            with zipfile.ZipFile(archive,'w') as z: z.writestr('readme.txt','public example')
            self.assertFalse(scan_artifact(archive))
            with zipfile.ZipFile(archive,'w') as z: z.writestr('../outside','example')
            self.assertTrue(scan_artifact(archive))

    def test_scanner_checks_staged_blob_even_if_worktree_was_cleaned(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def git(*args): return subprocess.run(['git',*args],cwd=root,check=True,capture_output=True)
            git('init','-b','main')
            git('config','user.name','Example'); git('config','user.email','example@example.invalid')
            (root/'readme.txt').write_text('public'); git('add','.'); git('commit','-m','baseline')
            (root/'sample.txt').write_text('sk'+'-'+'b'*30); git('add','sample.txt')
            (root/'sample.txt').write_text('public')
            findings,_=scan_repository(root)
            self.assertTrue(any('staged/history' in finding for finding in findings))

if __name__=='__main__': unittest.main()
