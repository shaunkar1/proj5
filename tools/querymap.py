"""Offline: for each long TPC test, run it on one query at a time at the pinned
commit and record the lines, entered functions and classes each query reaches,
and its CPU time.  Usage: querymap.py <sqlglot checkout> <out.json> [parallel]"""
import json, os, sys, time

ROOT, OUT = os.path.abspath(sys.argv[1]), sys.argv[2]
PAR = int(sys.argv[3]) if len(sys.argv) > 3 else 2
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import inlinepool, tpc
import coverage, unittest, unittest.mock, duckdb, numpy, pandas, pandas.testing  # noqa

os.chdir(ROOT); sys.path.insert(0, ROOT); os.environ["SKIP_INTEGRATION"] = "0"
inlinepool.install()
import importlib
module = importlib.import_module(tpc.MODULE)
counts = {"tpcds_sqls": len(list(module.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))),
          "tpch_sqls": len(list(module.load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql")))}
TMP = OUT + ".parts"
os.makedirs(TMP, exist_ok=True)


def run_one(method, position, traced):
    """In a fresh child: the test thinned to one query (or none, position -1)."""
    started, inst = set(), set()
    if traced:
        mon = sys.monitoring
        mon.use_tool_id(4, "q")
        def on_start(code, offset):
            f = code.co_filename
            if f.startswith(ROOT + os.sep):
                started.add((f, code.co_firstlineno))
            if code.co_name != "__init__" or not code.co_varnames:
                return mon.DISABLE
            try:
                t = type(sys._getframe(1).f_locals.get(code.co_varnames[0]))
                for k in t.__mro__:
                    if k.__module__.startswith(("sqlglot", "tests")):
                        inst.add(k.__module__ + ":" + k.__qualname__)
            except Exception:
                pass
        mon.register_callback(4, mon.events.PY_START, on_start)
        mon.set_events(4, mon.events.PY_START)
        cov = coverage.Coverage(data_file=None, source=[os.path.join(ROOT, "sqlglot"), os.path.join(ROOT, "tests")], config_file=False)
        cov.start()
    tpc.thin(module, {method: {position} if position >= 0 else set()})
    suite = unittest.TestSuite([getattr(module, tpc.CLASS)(method)])
    result = unittest.TestResult()
    t0 = time.process_time()
    devnull = os.open(os.devnull, os.O_RDWR); os.dup2(devnull, 1); os.dup2(devnull, 2)
    suite.run(result)
    cpu = time.process_time() - t0
    ok = result.wasSuccessful()
    record = {"ok": ok, "cpu": round(cpu, 3)}
    if traced:
        cov.stop(); sys.monitoring.set_events(4, 0)
        data = cov.get_data()
        record["lines"] = {os.path.relpath(f, ROOT): sorted(data.lines(f) or []) for f in data.measured_files() if data.lines(f)}
        record["started"] = sorted([os.path.relpath(f, ROOT), l] for f, l in started)
        record["inst"] = sorted(inst)
    with open(os.path.join(TMP, "%s.%d.%d.json" % (method, position, int(traced))), "w") as f:
        json.dump(record, f)


todo = []
for method, (attr, _schema) in tpc.TESTS.items():
    for position in range(-1, counts[attr]):
        for traced in (0, 1):
            if not os.path.exists(os.path.join(TMP, "%s.%d.%d.json" % (method, position, traced))):
                todo.append((method, position, traced))
active = set()
t0 = time.time()
for n, job in enumerate(todo):
    while len(active) >= PAR:
        pid, _ = os.wait(); active.discard(pid)
    pid = os.fork()
    if pid == 0:
        try:
            run_one(*job)
        finally:
            os._exit(0)
    active.add(pid)
    if n % 40 == 0:
        print(n, len(todo), round(time.time() - t0), file=sys.stderr, flush=True)
while active:
    pid, _ = os.wait(); active.discard(pid)

out = {}
for method, (attr, _schema) in tpc.TESTS.items():
    n = counts[attr]
    lines, started, inst, cpu = {}, {}, {}, []
    base = json.load(open(os.path.join(TMP, "%s.-1.0.json" % method)))
    assert base["ok"], method
    for position in range(n):
        timed = json.load(open(os.path.join(TMP, "%s.%d.0.json" % (method, position))))
        traced = json.load(open(os.path.join(TMP, "%s.%d.1.json" % (method, position))))
        assert timed["ok"] and traced["ok"], (method, position)
        cpu.append(round(max(timed["cpu"] - base["cpu"], 0.0), 3))
        bit = 1 << position
        for f, ls in traced["lines"].items():
            d = lines.setdefault(f, {})
            for l in ls:
                d[l] = d.get(l, 0) | bit
        for f, l in traced["started"]:
            d = started.setdefault(f, {})
            d[l] = d.get(l, 0) | bit
        for c in traced["inst"]:
            inst[c] = inst.get(c, 0) | bit
    out[method] = {"n": n, "cpu": cpu, "base_cpu": base["cpu"],
                   "lines": {f: {str(l): hex(m) for l, m in d.items()} for f, d in lines.items()},
                   "started": {f: {str(l): hex(m) for l, m in d.items()} for f, d in started.items()},
                   "inst": {c: hex(m) for c, m in inst.items()}}
    print(method, n, "queries, cpu", round(sum(cpu), 1), "s, base", base["cpu"], file=sys.stderr)
json.dump(out, open(OUT, "w"))
