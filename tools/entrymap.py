"""Offline: for the costlier fixture-driven tests, run each with one fixture
entry at a time at the pinned commit and record what that entry reaches and its
CPU time.  Usage: entrymap.py <sqlglot checkout> <tests.json> <times.json> <thinnable.json> <out.json> [parallel]"""
import json, os, sys, time

ROOT, TESTS, TIMES, THIN, OUT = os.path.abspath(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], os.path.abspath(sys.argv[5])
PAR = int(sys.argv[6]) if len(sys.argv) > 6 else 2
MIN_CPU = float(os.environ.get("MIN_CPU", "0.12"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import runner, thinload
import coverage, unittest, unittest.mock, duckdb, numpy, pandas, pandas.testing  # noqa

runner.PINNED_FIXTURES = os.path.join(ROOT, "tests", "fixtures")
tests = json.load(open(TESTS))["tests"]
number = {t["id"]: t["number"] for t in tests}
times = json.load(open(TIMES))["tests"]
targets = [t for t in json.load(open(THIN)) if times.get(t, 0) >= MIN_CPU and not t.startswith("tests/test_executor.py")]
TMP = OUT + ".parts"
os.makedirs(TMP, exist_ok=True)


def name_of(test_id):
    return test_id.replace("/", "_").replace("::", ".")


def run_one(test_id, position, traced):
    """position -2: count the entries; -1: none; k: only entry k."""
    started, inst = set(), set()
    cov = None
    if traced:
        mon = sys.monitoring
        mon.use_tool_id(4, "e")
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
    keep = set(range(10**7)) if position == -2 else ({position} if position >= 0 else set())
    rfd, wfd = os.pipe()
    t0 = time.process_time()
    runner.child_main(ROOT, [(0, test_id)], wfd, thin={test_id: ("keep", keep)})
    cpu = time.process_time() - t0
    os.close(wfd)
    status = os.read(rfd, 65536).decode()
    record = {"ok": "P 0" in status, "cpu": round(cpu, 4), "count": thinload.STATE["count"]}
    if traced:
        cov.stop(); sys.monitoring.set_events(4, 0)
        data = cov.get_data()
        record["lines"] = {os.path.relpath(f, ROOT): sorted(data.lines(f) or []) for f in data.measured_files() if data.lines(f)}
        record["started"] = sorted([os.path.relpath(f, ROOT), l] for f, l in started)
        record["inst"] = sorted(inst)
    with open(os.path.join(TMP, "%s.%d.%d.json" % (name_of(test_id), position, int(traced))), "w") as f:
        json.dump(record, f)


def run_all(todo):
    active = set()
    t0 = time.time()
    for n, job in enumerate(todo):
        if os.path.exists(os.path.join(TMP, "%s.%d.%d.json" % (name_of(job[0]), job[1], job[2]))):
            continue
        while len(active) >= PAR:
            pid, _ = os.wait(); active.discard(pid)
        pid = os.fork()
        if pid == 0:
            try:
                run_one(*job)
            finally:
                os._exit(0)
        active.add(pid)
        if n % 200 == 0:
            print(n, len(todo), round(time.time() - t0), file=sys.stderr, flush=True)
    while active:
        pid, _ = os.wait(); active.discard(pid)


def load(test_id, position, traced):
    return json.load(open(os.path.join(TMP, "%s.%d.%d.json" % (name_of(test_id), position, traced))))

run_all([(t, -2, 0) for t in targets])
counts = {t: load(t, -2, 0)["count"] for t in targets}
targets = [t for t in targets if counts[t] > 1]
print({t.split("::")[-1]: counts[t] for t in targets}, file=sys.stderr)
todo = [(t, k, traced) for t in targets for k in range(-1, counts[t]) for traced in (0, 1)]
run_all(todo)

out = {}
for t in targets:
    n = counts[t]
    base = load(t, -1, 0)
    lines, started, inst, cpu, ok = {}, {}, {}, [], base["ok"]
    for k in range(n):
        timed, traced = load(t, k, 0), load(t, k, 1)
        ok = ok and timed["ok"] and traced["ok"]
        cpu.append(round(max(timed["cpu"] - base["cpu"], 0.0), 4))
        bit = 1 << k
        for f, ls in traced["lines"].items():
            d = lines.setdefault(f, {})
            for l in ls:
                d[l] = d.get(l, 0) | bit
        for f, l in traced["started"]:
            d = started.setdefault(f, {})
            d[l] = d.get(l, 0) | bit
        for c in traced["inst"]:
            inst[c] = inst.get(c, 0) | bit
    if not ok:
        print("not usable (an entry fails alone):", t, file=sys.stderr)
        continue
    out[t] = {"n": n, "cpu": cpu, "base_cpu": base["cpu"],
              "lines": {f: {str(l): hex(m) for l, m in d.items()} for f, d in lines.items()},
              "started": {f: {str(l): hex(m) for l, m in d.items()} for f, d in started.items()},
              "inst": {c: hex(m) for c, m in inst.items()}}
    print(t.split("::")[-1], n, "entries, cpu", round(sum(cpu), 2), "base", base["cpu"], file=sys.stderr)
json.dump(out, open(OUT, "w"))
