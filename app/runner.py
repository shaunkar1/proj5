"""Run a chosen set of sqlglot tests against a patched tree, in a forked child.

The parent keeps third-party modules (duckdb, pandas, numpy) imported, so a
child only pays for importing the patched sqlglot and the test modules it
needs.  Tests run through unittest directly; a test counts as failed when it
or any of its subtests fails or errors, when its class or module fixture
fails, when its file cannot be imported, or when it hangs or kills the child.
"""

import importlib
import os
import select
import signal
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLOSE_IN_CHILD = []  # descriptors a child must not keep (the answer stream)
PINNED_FIXTURES = None  # tests/fixtures of the pinned tree, set by the caller
PROXY = "proxy:"


def split_id(test_id):
    """tests/a/test_b.py::Class::test_name -> (module, class, method)."""
    path, cls, method = test_id.split("::", 2)
    module = path[:-3].replace("/", ".")
    return module, cls, method


class _Result(unittest.TestResult):
    """Reports each test's outcome to the parent as it happens."""

    def __init__(self, emit, index_of, thin=None):
        super().__init__()
        self._thin = thin or {}
        self._timing = bool(os.environ.get("BR_TIMES"))
        self._t0 = 0.0
        self.failfast = True  # makes a failed subtest end its test early
        self._emit = emit
        self._index_of = index_of
        self._failed = False
        self._current = None

    def stop(self):  # failfast must not stop the remaining tests
        pass

    def startTest(self, test):
        super().startTest(test)
        self._failed = False
        self._current = self._index_of.get(test.id())
        if self._current is not None:
            self._emit("S %d\n" % self._current)
        if self._thin:
            import thinload

            thinload.begin(self._thin.get(test.id()))
        if self._timing:
            self._t0 = time.process_time()

    def stopTest(self, test):
        super().stopTest(test)
        if self._thin:
            import thinload

            thinload.end()
        if self._current is not None:
            if self._timing:
                self._emit("T %d %.4f\n" % (self._current, time.process_time() - self._t0))
            self._emit("%s %d\n" % ("F" if self._failed else "P", self._current))
        self._current = None

    def _bad(self, test):
        if isinstance(test, unittest.TestCase) and test.id() in self._index_of:
            self._failed = True
        else:
            # A class or module fixture failed: report it by name.
            self._emit("X %s\n" % (getattr(test, "description", None) or str(test)).replace("\n", " "))

    def addError(self, test, err):
        self._bad(test)

    def addFailure(self, test, err):
        self._bad(test)

    def addUnexpectedSuccess(self, test):
        self._bad(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._bad(test)

    def addSuccess(self, test):
        pass

    def addSkip(self, test, reason):
        pass

    def addExpectedFailure(self, test, err):
        pass


class _FirstRowsCsv:
    """csv module stand-in for tests/test_executor.py: its class fixture loads
    every row of every TPC table, which only the two long-running TPC tests
    (never run here) look at.  Reading the header and one row keeps the fixture's
    code path and skips the bulk."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def reader(self, *args, **kwargs):
        import itertools

        return itertools.islice(self._real.reader(*args, **kwargs), 2)


def _limit_duckdb_threads():
    """duckdb starts one thread per host core for every connection; under a
    process limit that can fail, and here it only burns CPU."""
    duckdb = sys.modules.get("duckdb")
    if duckdb is None or getattr(duckdb.connect, "_limited", False):
        return
    real_connect = duckdb.connect

    def connect(*args, **kwargs):
        config = dict(kwargs.get("config") or {})
        config.setdefault("threads", 2)
        kwargs["config"] = config
        return real_connect(*args, **kwargs)

    connect._limited = True
    duckdb.connect = connect


def child_main(root, tests, wfd, extra_setup=None, short_tables=False, thin=None, before_run=None):
    """Runs in the forked child.  `tests` is a list of (index, test_id)."""
    for fd in CLOSE_IN_CHILD:
        try:
            os.close(fd)
        except OSError:
            pass
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)  # nothing a test prints may reach the answer stream
    if not os.environ.get("BR_DEBUG"):
        os.dup2(devnull, 2)
        sys.stderr = open(os.devnull, "w")
    sys.stdout = open(os.devnull, "w")

    def emit(text):
        os.write(wfd, text.encode())

    os.chdir(root)
    sys.path.insert(0, root)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTHONPATH"] = root
    sys.dont_write_bytecode = True
    sys.path.insert(0, HERE)
    import inlinepool

    inlinepool.install()
    _limit_duckdb_threads()
    if extra_setup is not None:
        extra_setup()

    thin_by_case = {}
    if thin:
        # filter what the fixture loaders give to the tests named in `thin`
        try:
            import thinload

            thinload.install(importlib.import_module("tests.helpers"), PINNED_FIXTURES)
        except BaseException:
            thin = None
    suite = unittest.TestSuite()
    index_of = {}
    broken_modules = set()
    stand_ins = [(index, test_id[len(PROXY):]) for index, test_id in tests if test_id.startswith(PROXY)]
    tests = [(index, test_id) for index, test_id in tests if not test_id.startswith(PROXY)]
    # looping tests thinned to the part the patch can reach, "<test id>|<positions>":
    # they join the ordinary tests so that class fixtures are set up once
    query_runs = [(index, spec[len("query:"):]) for index, spec in stand_ins if spec.startswith("query:")]
    thinned = {}
    if query_runs:
        try:
            import tpc

            selection, modules = {}, {}
            for index, spec in query_runs:
                test_id, _, positions = spec.rpartition("|")
                selection[test_id] = {int(p) for p in positions.split(",") if p}
                modules[tpc.TARGETS[test_id][0]] = importlib.import_module(tpc.TARGETS[test_id][0])
            tpc.thin(modules, selection)
            if "tests.test_executor" in modules:
                short_tables = False  # the thinning decides which tables load in full
            for index, spec in query_runs:
                thinned[index] = spec.rpartition("|")[0]
            tests = sorted(tests + [(index, test_id) for index, test_id in thinned.items()], key=lambda entry: (entry[1].split("::")[0], entry[0] if entry[0] >= 0 else 10**9))
        except BaseException:
            for index, _spec in query_runs:
                emit("F %d\n" % index)
            thinned = {}
    for index, test_id in tests:
        module_name, class_name, method = split_id(test_id)
        if module_name in broken_modules:
            emit("F %d\n" % index)
            continue
        try:
            module = importlib.import_module(module_name)
            if short_tables and module_name == "tests.test_executor" and not isinstance(module.csv, _FirstRowsCsv):
                module.csv = _FirstRowsCsv(module.csv)
            cls = getattr(module, class_name)
            case = cls(method)
        except BaseException:
            if module_name not in sys.modules:
                broken_modules.add(module_name)
            emit("F %d\n" % index)
            continue
        index_of[case.id()] = index
        if thin and test_id in thin:
            thin_by_case[case.id()] = thin[test_id]
        suite.addTest(case)
    result = _Result(emit, index_of, thin_by_case)
    if before_run is not None:
        before_run()
    try:
        suite.run(result)
    except BaseException:
        pass
    stand_ins = [(index, test_id) for index, test_id in stand_ins if not test_id.startswith("query:")]
    if stand_ins:
        import proxies

        for index, test_id in stand_ins:
            emit("S %d\n" % index)
            try:
                if test_id.startswith("import:"):
                    importlib.import_module(test_id[len("import:"):])
                else:
                    proxies.PROXIES[test_id]()
                emit("P %d\n" % index)
            except BaseException:
                emit("F %d\n" % index)
    emit("D\n")


class Job:
    """One child process working through a list of tests."""

    def __init__(self, key, root, tests, budgets, extra_setup=None, short_tables=False, thin=None):
        self.short_tables = short_tables
        self.thin = thin
        self.key = key
        self.root = root
        self.pending = list(tests)  # (index, test_id), in running order
        self.budgets = budgets  # index -> seconds allowed for that test
        self.extra_setup = extra_setup
        self.failed = set()
        self.passed = set()
        self.pid = None
        self.fd = None
        self.buffer = b""
        self.current = None
        self.deadline = None
        self.class_of = {index: test_id.rsplit("::", 1)[0] for index, test_id in tests}
        self.restarts = 0
        self.cpu = 0.0
        self.times = {}
        self.kills = 0
        self.elapsed = {}  # index -> wall seconds the test took
        self.started_at = 0.0

    def start(self):
        rfd, wfd = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(rfd)
                os.setpgid(0, 0)
                child_main(self.root, self.pending, wfd, self.extra_setup, self.short_tables, self.thin)
            except BaseException:
                pass
            finally:
                os._exit(0)
        os.close(wfd)
        self.pid = pid
        self.fd = rfd
        self.buffer = b""
        self.current = None
        self.deadline = time.monotonic() + 20.0  # for imports before the first test

    def _done(self, index, failed):
        (self.failed if failed else self.passed).add(index)
        self.pending = [(i, t) for i, t in self.pending if i != index]

    def feed(self, data):
        """Handle output from the child; returns True when the child said it is finished."""
        self.buffer += data
        finished = False
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            kind, _, rest = line.decode("utf-8", "replace").partition(" ")
            if kind == "S":
                self.current = int(rest)
                self.started_at = time.monotonic()
                self.deadline = self.started_at + self.budgets.get(self.current, 20.0)
            elif kind in ("P", "F"):
                self.elapsed[int(rest)] = time.monotonic() - self.started_at
                self._done(int(rest), kind == "F")
                self.current = None
                self.deadline = time.monotonic() + 20.0
            elif kind == "X":
                # "setUpClass (tests.test_x.TestX)" and friends: every test of that
                # class (or module) in this job fails.
                name = rest[rest.find("(") + 1 : rest.rfind(")")] if "(" in rest else rest
                target = name.replace(".", "/")
                for index, test_id in list(self.pending):
                    module_name, class_name, _ = split_id(test_id)
                    full = (module_name + "." + class_name).replace(".", "/")
                    if full == target or module_name.replace(".", "/") == target:
                        if rest.startswith("tearDown"):
                            continue
                        self._done(index, True)
                if rest.startswith("tearDown"):
                    for index in list(self.passed):
                        test_id_class = self.class_of[index]
                        module_name, class_name, _ = split_id(test_id_class + "::x")
                        full = (module_name + "." + class_name).replace(".", "/")
                        if full == target or module_name.replace(".", "/") == target:
                            self.passed.discard(index)
                            self.failed.add(index)
            elif kind == "T":
                index, _, cpu = rest.partition(" ")
                self.times[int(index)] = float(cpu)
            elif kind == "D":
                finished = True
        return finished

    def kill(self):
        if self.pid is None:
            return
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except OSError:
                pass

    def reap(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.pid is not None:
            try:
                os.killpg(self.pid, signal.SIGKILL)  # stragglers the tests started
            except OSError:
                pass
            try:
                _, _, usage = os.wait4(self.pid, 0)
                self.cpu += usage.ru_utime + usage.ru_stime
            except OSError:
                pass
            self.pid = None

    def interrupted(self):
        """The child died or was killed.  Blame the running test and carry on."""
        self.reap()
        if self.current is not None:
            self._done(self.current, True)
        elif self.pending:
            # Died outside any test (imports, fixtures): blame the next one.
            self._done(self.pending[0][0], True)
        self.restarts += 1
        if self.restarts > 6:
            for index, _ in list(self.pending):
                self._done(index, True)


def _rss_mb(pid):
    try:
        with open("/proc/%d/statm" % pid) as handle:
            return int(handle.read().split()[1]) * 4096 // (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return 0


def run_jobs(jobs, parallel=2, stop_at=None, rss_limit_mb=1400, log=None):
    """Run jobs, `parallel` at a time.  Jobs not finished by `stop_at`
    (time.monotonic()) keep whatever is still pending in job.pending."""
    queue = list(jobs)
    active = {}
    while queue or active:
        now = time.monotonic()
        if stop_at is not None and now >= stop_at:
            for job in active.values():
                job.kill()
                job.reap()
            return False
        while queue and len(active) < parallel:
            job = queue.pop(0)
            if not job.pending:
                continue
            job.start()
            active[job.fd] = job
        if not active:
            continue
        ready, _, _ = select.select(list(active), [], [], 0.05)
        for fd in ready:
            job = active[fd]
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            finished = job.feed(data) if data else False
            if finished or not data:
                del active[fd]
                if finished:
                    job.reap()
                    for index, _ in list(job.pending):  # not reported: be safe
                        job._done(index, True)
                else:
                    job.interrupted()
                    if job.pending:
                        queue.insert(0, job)
        now = time.monotonic()
        for fd, job in list(active.items()):
            too_big = _rss_mb(job.pid) > rss_limit_mb
            if now > job.deadline or too_big:
                if log:
                    log("killing child of %s (%s) at test %s" % (job.key, "memory" if too_big else "time", job.current))
                job.kill()
                del active[fd]
                job.interrupted()
                job.kills += 1
                if job.kills >= 2:
                    # the patch hangs or balloons whatever reaches it: stop paying for
                    # proof and treat everything left as failing
                    for index, _ in list(job.pending):
                        job._done(index, True)
                if job.pending:
                    queue.insert(0, job)
    return True
