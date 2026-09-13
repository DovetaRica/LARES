"""Bounded offline benchmark. All database writes use temporary synthetic data."""
import argparse
import importlib.util
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from home_ai.database import Database
from home_ai.pipeline import Pipeline
from home_ai.config import load_config


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-db", type=Path)
    parser.add_argument("--rows", type=int, default=1000)
    args=parser.parse_args()
    if not 1 <= args.rows <= 10000: parser.error("rows must be 1..10000")
    baseline=Database
    if args.baseline_db:
        spec=importlib.util.spec_from_file_location("baseline_database",args.baseline_db)
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        baseline=module.Database
    row={"ts":"2025-01-01T00:00:00Z","entity_id":"example.device","domain":"device","category":"device",
         "event_type":"state_changed","new_state":"on","source":"system","attributes_json":{},"metadata_json":{}}
    timings={"baseline_seconds":[],"batch_seconds":[]}
    with tempfile.TemporaryDirectory(prefix="home-ai-benchmark-") as folder:
        for run in range(3):
            old=baseline(Path(folder)/f"old-{run}.db")
            start=time.perf_counter()
            for _ in range(args.rows): old.insert_event(row)
            timings["baseline_seconds"].append(time.perf_counter()-start)
            new=Database(Path(folder)/f"new-{run}.db")
            start=time.perf_counter()
            with new.batch():
                for _ in range(args.rows): new.insert_event(row)
            timings["batch_seconds"].append(time.perf_counter()-start)
            assert old.scalar("SELECT COUNT(*) FROM events") == new.scalar("SELECT COUNT(*) FROM events") == args.rows
    pipeline=Pipeline(load_config())
    start=time.perf_counter()
    for i in range(10000):
        pipeline.feed({"id":str(i),"ts":(datetime(2025,1,1,tzinfo=timezone.utc)+timedelta(seconds=i)).isoformat(),
                       "entity_id":f"example.device{i%1000}","state":str(i),"source":"system","kind":"device"})
    pipeline_seconds=time.perf_counter()-start
    result={"python":platform.python_version(),"os":platform.system(),"cpus":os.cpu_count(),"rows_per_run":args.rows,
            "same_schema":True,**timings,"median_speedup":statistics.median(timings["baseline_seconds"])/statistics.median(timings["batch_seconds"]),
            "pipeline_seconds_10000_events":pipeline_seconds,"pipeline_metrics":pipeline.metrics()}
    print(json.dumps(result,indent=2))

if __name__=="__main__": main()
