"""Offline: which fixture-driven tests still pass at the pinned commit when the
fixture loaders give them nothing?  Those are the tests whose loop entries can
be thinned.  Usage: thincheck.py <sqlglot checkout> <tests.json> <iso dir> <out.json>"""
import glob, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import runner
import unittest, unittest.mock, duckdb, numpy, pandas, pandas.testing  # noqa

ROOT, TESTS, ISO, OUT = os.path.abspath(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
runner.PINNED_FIXTURES = os.path.join(ROOT, "tests", "fixtures")
tests = json.load(open(TESTS))["tests"]
number = {t["id"]: t["number"] for t in tests}
SKIP = ("test_max_nodes", "test_execute_tpcds", "test_execute_tpch", "test_optimized_tpch")
users = []
for path in glob.glob(os.path.join(ISO, "*.json")):
    r = json.load(open(path))
    if any(o.startswith("tests/fixtures/") and not o.endswith(".gz") for o in r["opened"]) and not r["id"].endswith(SKIP):
        users.append(r["id"])
jobs = [runner.Job(t, ROOT, [(number[t], t)], {}, short_tables=True, thin={t: ("changed", set())}) for t in sorted(users)]
runner.run_jobs(jobs, parallel=2)
ok = sorted(j.key for j in jobs if not j.failed)
json.dump(ok, open(OUT, "w"))
print(len(users), "fixture-driven tests,", len(ok), "pass with no entries; not thinnable:", sorted(j.key for j in jobs if j.failed))
