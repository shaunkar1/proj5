"""Offline: CPU seconds each test takes through the selector's runner at the
pinned commit, and the extra each test class costs to set up.
Usage: timepass.py <sqlglot checkout> <tests.json> <out.json>"""
import json, os, sys
os.environ["BR_TIMES"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import runner
import unittest, unittest.mock, duckdb, numpy, pandas, pandas.testing  # noqa

ROOT, TESTS, OUT = os.path.abspath(sys.argv[1]), sys.argv[2], sys.argv[3]
tests = json.load(open(TESTS))["tests"]
SKIP = ("test_max_nodes", "test_execute_tpcds", "test_execute_tpch", "test_optimized_tpch")
by_class = {}
for t in tests:
    if not t["id"].endswith(SKIP):
        by_class.setdefault(t["id"].rsplit("::", 1)[0], []).append((t["number"], t["id"]))
jobs = [runner.Job(c, ROOT, entries, {}, short_tables=True) for c, entries in by_class.items()]
runner.run_jobs(jobs, parallel=2)
times, setup = {}, {}
for job in jobs:
    assert not job.failed, job.key
    for n, test_id in by_class[job.key]:
        times[test_id] = round(job.times[n], 4)
    setup[job.key] = round(max(job.cpu - sum(job.times.values()), 0.0), 3)
json.dump({"tests": times, "classes": setup}, open(OUT, "w"))
print("timed", len(times), "tests", round(sum(times.values()), 1), "s; class overhead", round(sum(setup.values()), 1), "s")
print(sorted(setup.items(), key=lambda kv: -kv[1])[:6])
