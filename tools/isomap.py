"""Offline map builder: run every test of the pinned commit alone, in a fresh
process, and record the lines it executes, the files it opens and the sqlglot
classes it instantiates.  Usage: isomap.py <sqlglot checkout> <tests.json> <out dir> [parallel]"""
import builtins, io, json, os, sys, time

ROOT, TESTS, OUT = os.path.abspath(sys.argv[1]), sys.argv[2], os.path.abspath(sys.argv[3])
PAR = int(sys.argv[4]) if len(sys.argv) > 4 else 2
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import runner
import coverage
import unittest, unittest.mock, duckdb, numpy, pandas, pandas.testing  # noqa

os.makedirs(OUT, exist_ok=True)
tests = json.load(open(TESTS))["tests"]


def child(index, test_id):
    opened = set()
    inst = set()
    _open = builtins.open

    def traced_open(file, *a, **k):
        try:
            if not isinstance(file, int):
                p = os.path.abspath(os.fspath(file))
                if p.startswith(ROOT + os.sep) and "__pycache__" not in p:
                    opened.add(os.path.relpath(p, ROOT))
        except Exception:
            pass
        return _open(file, *a, **k)

    builtins.open = traced_open
    io.open = traced_open
    mon = sys.monitoring
    mon.use_tool_id(4, "inst")

    started = set()

    def on_start(code, offset):
        f = code.co_filename
        if f.startswith(ROOT + os.sep):
            started.add((f, code.co_firstlineno))
        if code.co_name != "__init__" or not code.co_varnames:
            return mon.DISABLE
        try:
            t = type(sys._getframe(1).f_locals.get(code.co_varnames[0]))
            if t not in inst:
                for k in t.__mro__:
                    if k.__module__.startswith(("sqlglot", "tests")):
                        inst.add(k)
        except Exception:
            pass

    mon.register_callback(4, mon.events.PY_START, on_start)
    mon.set_events(4, mon.events.PY_START)
    cov = coverage.Coverage(data_file=None, source=[os.path.join(ROOT, "sqlglot"), os.path.join(ROOT, "tests")], config_file=False)
    cov.start()
    rfd, wfd = os.pipe()
    t0 = time.process_time()
    runner.child_main(ROOT, [(index, test_id)], wfd)
    cpu = time.process_time() - t0
    cov.stop()
    mon.set_events(4, 0)
    os.close(wfd)
    status = os.read(rfd, 65536).decode()
    data = cov.get_data()
    lines = {os.path.relpath(f, ROOT): sorted(data.lines(f) or []) for f in data.measured_files()}
    lines = {f: l for f, l in lines.items() if l}
    with _open(os.path.join(OUT, "%d.json" % index), "w") as f:
        json.dump({"id": test_id, "status": status, "cpu": round(cpu, 3), "lines": lines, "opened": sorted(opened), "started": sorted([os.path.relpath(f, ROOT), l] for f, l in started),
                   "inst": sorted(c.__module__ + ":" + c.__qualname__ for c in inst)}, f)


todo = [(t["number"], t["id"]) for t in tests if not os.path.exists(os.path.join(OUT, "%d.json" % t["number"]))]
only = os.environ.get("ONLY")
if only:
    todo = [x for x in todo if only in x[1]]
active = set()
start = time.time()
for n, (index, test_id) in enumerate(todo):
    while len(active) >= PAR:
        pid, _ = os.wait()
        active.discard(pid)
    pid = os.fork()
    if pid == 0:
        try:
            child(index, test_id)
        finally:
            os._exit(0)
    active.add(pid)
    if n % 100 == 0:
        print(n, len(todo), round(time.time() - start), file=sys.stderr, flush=True)
while active:
    pid, _ = os.wait()
    active.discard(pid)
print("done", round(time.time() - start), file=sys.stderr)
