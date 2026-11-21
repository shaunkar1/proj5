"""Blast Radius selector.

For every patch: find the tests whose execution reaches the changed code
(analyze.py), run the cheap ones against the patched tree (runner.py) and keep
those that fail, then spread the selection over workers.  Whenever a step
cannot be trusted - an odd patch, a time budget running out - the fallback is
to select more tests, never fewer.
"""

import json
import os
import sys
import time

START = time.monotonic()
for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_name, "1")  # the run has a process limit; keep thread pools small
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DATA = os.environ.get("BR_DATA", "/data")
REPO = os.environ.get("BR_REPO", "/app/repo")
MAPS = os.environ.get("BR_MAPS", os.path.join(HERE, "..", "maps", "map.bin"))
TMP = os.environ.get("BR_TMP", "/tmp")
DUCK = os.environ.get("BR_DUCK", os.path.join(HERE, "..", "cache", "duck.bin"))
VERBOSE = os.environ.get("BR_VERBOSE") == "1"
PARALLEL = int(os.environ.get("BR_PARALLEL") or 2)

WORKER_MS = 2000
MAX_WORKERS = 8

MAX_NODES = "tests/test_parser.py::TestParser::test_max_nodes"
GENERATOR_MEMORY = "tests/test_generator.py::TestGenerator::test_generator_memory"
EXECUTOR_HEAVY = (
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch",
)
EXECUTOR_PATHS = ("tests/test_executor.py", "tests/fixtures/optimizer/tpc-")
MIN_EVIDENCE = 3
SPEED = 1.0
SLACK = 1.0
EXACT_BUDGET_FAST = 5.0    # the same, when DuckDB's answers are on record (costs are then measured in-suite)
EXACT_BUDGET = 12.0        # CPU seconds worth spending to settle a long TPC test outright
PROBE_QUERIES = 10         # executing queries to try when a long TPC test cannot be settled
SETTLE_ODDS = 0.6          # chance that a long test reached by a change still passes
STRONG_EVIDENCE = 4        # this many cheaper failures select it even after a clean probe
FAIL_ODDS = 0.5            # chance that a reachable change breaks at least one test
FAIL_SHARE = 0.3           # share of the candidates' worker time the failing ones need


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


def pack(numbers, millis):
    """Split tests over workers: longest first onto the lightest worker, for the
    worker count that makes busiest-load + 2 s per worker smallest."""
    numbers = sorted(numbers, key=lambda n: (-millis[n], n))
    if not numbers:
        return []
    best = None
    for count in range(1, min(MAX_WORKERS, len(numbers)) + 1):
        loads = [0] * count
        workers = [[] for _ in range(count)]
        for n in numbers:
            i = loads.index(min(loads))
            loads[i] += millis[n]
            workers[i].append(n)
        cost = max(loads) + WORKER_MS * count
        if best is None or cost < best[0]:
            best = (cost, workers)
    return [sorted(w) for w in best[1] if w]


def pack_cost(numbers, millis):
    workers = pack(numbers, millis)
    if not workers:
        return 0
    return max(sum(millis[n] for n in w) for w in workers) + WORKER_MS * len(workers)


DUCK_INPUTS = ("tests/test_executor.py", "tests/helpers.py", "tests/__init__.py", "tests/fixtures/optimizer/tpc-")


def through_code_only(plan, maps, mask):
    """True when the patch can reach the tests in `mask` (a bit mask over the
    map's tests) only through sqlglot's own source: every other touched file is
    a data file none of them reads, or an existing test-side module none of
    them loads."""
    for path in plan.touched:
        if path.startswith("sqlglot/") and path.endswith(".py"):
            continue
        if path.startswith("tests/fixtures/") and path in plan.fixture:
            if plan.fixture[path] & mask:
                return False
            continue
        if path.startswith("tests/") and path.endswith(".py") and plan.files.get(path) is not None and os.path.isfile(os.path.join(REPO, path)):
            loaders = maps.anyline.get(path, 0) | maps.opened.get(path, 0) | maps.by_file.get(path, 0)
            if os.path.basename(path) == "__init__.py":
                loaders |= maps.package_users(os.path.dirname(path))
            if loaders & mask:
                return False
            continue
        return False
    return True


def query_cpu_of(maps, method, position):
    return maps.queries[method][1][position]


def bits(mask):
    out = []
    i = 0
    while mask:
        if mask & 1:
            out.append(i)
        mask >>= 1
        i += 1
    return out


def machine_speed():
    """Factor applied to CPU estimates before they are weighed against simulated
    seconds.  Measuring it at start-up (a timing loop) was tried and scored
    worse: it leaned toward selecting unseen, which costs more worker time
    than the CPU it saves.  Estimates are used as measured."""
    return float(os.environ.get("BR_SPEED") or 1.0)


def main(out):
    global SPEED, SLACK
    SPEED = machine_speed()
    SLACK = max(1.0, SPEED)  # wall-clock allowances stretch on a slower machine, never shrink
    log("machine speed factor %.2f" % SPEED)
    import analyze
    import overlay
    import proxies
    import runner
    import tpc

    with open(os.path.join(DATA, "tests.json")) as handle:
        tests = json.load(handle)["tests"]
    number_of = {t["id"]: t["number"] for t in tests}
    import astdiff

    astdiff.DOCS_MATTER = any("test_docs" in t["id"] or "doctest" in t["id"].lower() for t in tests)
    id_of = {t["number"]: t["id"] for t in tests}
    millis = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    seconds = {t["number"]: t["seconds"] for t in tests}
    everything = sorted(number_of.values())

    commits = sorted(name[:-6] for name in os.listdir(os.path.join(DATA, "commits")) if name.endswith(".patch"))
    answered = {}

    def answer(commit, numbers):
        answered[commit] = pack(numbers, millis)

    try:
        maps = analyze.Maps(MAPS)
        runner.PINNED_FIXTURES = os.path.join(REPO, "tests", "fixtures")
        # per-query costs of the long TPC tests when DuckDB's answers are on record
        fast_queries = {}
        if os.path.exists(DUCK):
            try:
                with open(os.path.join(os.path.dirname(MAPS), "fastcpu.json")) as handle:
                    fast = json.load(handle)
                fast_queries = dict(maps.queries)
                for method, sub in fast.items():
                    if method in maps.queries and len(sub["cpu"]) == maps.queries[method][0].size:
                        fast_queries[method] = (maps.queries[method][0], sub["cpu"], sub["base_cpu"])
            except Exception:
                fast_queries = {}
        # CPU seconds a test is expected to take here: measured alone at the pinned
        # commit (minus process start-up), else its recorded time
        estimate = dict(seconds)
        for i, test_id in enumerate(maps.ids):
            if test_id in number_of and maps.times[i] is not None:
                estimate[number_of[test_id]] = maps.times[i]
        # tests the maps know nothing about are treated as reachable from anything
        unknown = [n for test_id, n in number_of.items() if test_id not in set(maps.ids)]
        # the batch has 3 minutes of wall clock (4.5 in the final, which is about
        # one and a half times as large); stop running tests well before that
        limit = 180.0 if len(commits) <= 48 else 270.0
        stop_at = START + float(os.environ.get("BR_CUTOFF") or 0.62 * limit)
        # one lane while there is time to spare, two once a third of the limit is gone
        hurry_at = START + float(os.environ.get("BR_HURRY") or 0.3 * limit)

        plans = {}
        query_budget = {}
        jobs = []
        job_of = {}
        preload = set()
        for commit in commits:
            duck_ok = False
            try:
                with open(os.path.join(DATA, "commits", commit + ".patch"), "rb") as handle:
                    plan = analyze.analyze(maps, handle.read(), REPO)
            except Exception as error:  # never trust a failed analysis
                log(commit, "analysis failed:", repr(error))
                plan = analyze.Plan()
                plan.blind_all = True
            plans[commit] = plan
            if plan.blind_all:
                continue
            cands = {number_of[maps.ids[i]] for i in bits(plan.cands) if maps.ids[i] in number_of}
            blind = {number_of[maps.ids[i]] for i in bits(plan.blind) if maps.ids[i] in number_of}
            if cands or plan.files:
                cands.update(unknown) if cands else None
            plan.numbers = cands
            plan.blind_numbers = set(blind)
            heavy = set()
            plan.proxy = {}
            for n in cands:
                test_id = id_of[n]
                if test_id in tpc.TARGETS:
                    usable = test_id in maps.queries and not any(p.startswith(tpc.UNSTABLE[test_id]) for p in plan.touched)
                    if test_id == tpc.GENERATOR_MEMORY and not usable:
                        continue  # cheap enough to simply run in full with the rest
                    heavy.add(n)
                elif test_id in proxies.PROXIES and n not in blind:
                    if test_id not in maps.ids or not through_code_only(plan, maps, 1 << maps.ids.index(test_id)):
                        # the stand-in only exercises sqlglot's own code: anything
                        # else that reaches the test is not for it to judge
                        plan.blind_numbers.add(n)
                    else:
                        heavy.add(n)
                        plan.proxy[n] = test_id
            plan.heavy = heavy
            light = sorted(cands - heavy - plan.blind_numbers)
            plan.light = light
            plan.smoke = {}
            # Tests that are candidates only because their fixture file changed
            # need to see just the entries that differ from the pinned file.
            thin = {}
            cost_of = estimate
            if plan.fixture and "tests/helpers.py" not in plan.touched:
                cost_of = dict(estimate)
                for path, mask in plan.fixture.items():
                    name = path[len("tests/fixtures/"):]
                    for i in bits(mask & ~plan.other_mask):
                        test_id = maps.ids[i]
                        if test_id in maps.thinnable and test_id in number_of:
                            thin.setdefault(test_id, ("changed", set()))[1].add(name)
                            cost_of[number_of[test_id]] = min(estimate[number_of[test_id]], 0.05)
            # Code changes: fixture-driven tests need only the entries that reach them.
            if plan.plain and plan.python:
                for n in light:
                    test_id = id_of[n]
                    entry = maps.queries.get(test_id)
                    if entry is None or test_id in tpc.TARGETS or test_id not in maps.thinnable:
                        continue
                    mask = analyze.query_mask(maps, plan, test_id)
                    if mask is None:
                        continue
                    view, entry_cpu, base_cpu = entry
                    positions = bits(mask)
                    if len(positions) >= view.size:
                        continue
                    if cost_of is estimate:
                        cost_of = dict(estimate)
                    thin[test_id] = ("keep", set(positions))
                    cost_of[n] = min(estimate[n], base_cpu + sum(entry_cpu[k] for k in positions))
            # Running the cheap candidates costs their CPU time; selecting all of
            # them unseen costs worker time.  Take the cheaper road.
            # Running leaves nothing to select about half the time, and usually a
            # small part of the candidates otherwise.
            executor_heavy = {n for n in heavy if n not in plan.proxy}
            # DuckDB's answers for the TPC queries are on record when the patch
            # leaves tests and fixtures alone; the long tests then cost far less
            duck_ok = bool(fast_queries) and through_code_only(plan, maps, maps.by_file.get("tests/test_executor.py", 0)) and not any(p.startswith(DUCK_INPUTS) for p in plan.touched)
            Q = fast_queries if duck_ok else maps.queries
            exact_budget = EXACT_BUDGET_FAST if duck_ok else EXACT_BUDGET
            # The long TPC tests: which of their queries can see the patch, and
            # can those be run outright for a firm answer?
            plan.query = {}
            plan.untouched = set()
            for n in sorted(executor_heavy):
                method = id_of[n]
                entry = Q.get(method)
                if entry is None or n in plan.blind_numbers:
                    continue
                view, query_cpu, base_cpu = entry
                unstable = [p for p in plan.touched if p.startswith(tpc.UNSTABLE[method])]
                attr = tpc.TARGETS[method][3]
                fixture_path = "tests/fixtures/" + tpc.FIXTURES[attr] if attr else None
                index = maps.ids.index(method)
                if not unstable:
                    if (plan.other_mask >> index) & 1 or not plan.fixture:
                        mask = analyze.query_mask(maps, plan, method)
                    else:
                        mask = 0  # here only through a fixture file it shares a fixture with
                        for path, readers in plan.fixture.items():
                            if (readers >> index) & 1 and not path.startswith("tests/fixtures/optimizer/tpc-"):
                                mask = None
                    positions = bits(mask) if mask is not None else list(range(view.size))
                elif unstable == [fixture_path] and fixture_path in plan.fixture and not (plan.other_mask >> index) & 1:
                    # only its own query file changed: the queries whose text
                    # differs from the pinned file are the ones to run
                    try:
                        with open(os.path.join(REPO, fixture_path), "rb") as handle:
                            pinned_text = handle.read().decode("utf-8")
                        changed = tpc.changed_positions(pinned_text, plan.files[fixture_path].decode("utf-8"))
                    except Exception:
                        changed = None
                    if changed is None:
                        continue
                    positions = sorted(changed)
                else:
                    continue
                if not positions:
                    plan.untouched.add(n)  # no query or dialect of it sees the change
                    continue
                positions.sort(key=lambda k: query_cpu[k])
                total = base_cpu + sum(query_cpu[k] for k in positions)
                plan.query[n] = (method, positions, total)
            executor_heavy -= plan.untouched
            heavy -= plan.untouched
            settled, spent_exact = set(), 0.0
            for n in sorted(plan.query, key=lambda n: plan.query[n][2]):
                if (spent_exact + plan.query[n][2]) * SPEED <= exact_budget:
                    settled.add(n)
                    spent_exact += plan.query[n][2]
            unsettled = executor_heavy - settled
            # A test that cannot be settled is probed: every reaching query that
            # only gets parsed, plus the cheapest few that execute.  A patch that
            # breaks such a test nearly always breaks most of its queries.
            plan.probe = {}
            for n in sorted(unsettled):
                if n not in plan.query:
                    continue
                method, positions, _total = plan.query[n]
                view = Q[method][0]
                executing = view.inst.get("sqlglot.executor.python:PythonExecutor", 0)
                parsed_only = [k for k in positions if not (executing >> k) & 1]
                executed = [k for k in positions if (executing >> k) & 1][:PROBE_QUERIES]
                if executed or parsed_only:
                    plan.probe[n] = parsed_only + executed
            unprobed = unsettled - set(plan.probe)
            blind_cost = pack_cost(set(light) | unprobed | plan.blind_numbers, millis) / 1000.0
            cpu_cost = 0.08 + sum(cost_of[n] for n in light)
            cpu_cost += sum(maps.class_setup.get(c, 0.02) for c in {id_of[n].rsplit("::", 1)[0] for n in light})
            run_cost = cpu_cost * SPEED + FAIL_ODDS * (2.0 + FAIL_SHARE * max(blind_cost - 2.0, 0.0))
            plan.ran_light = True
            if os.environ.get("BR_TRACE"):
                log(commit, "weigh cpu %.2f blind %.2f" % (cpu_cost, blind_cost))
            if light and run_cost > blind_cost and not os.environ.get("BR_NOBLIND"):
                log(commit, "cheaper to select %d candidates unseen (%.1f s) than to run them (%.1f s)" % (len(light) + len(unprobed), blind_cost, run_cost))
                # A probe alone is not trusted: the cheap executor tests that reach
                # the change still run, as witnesses.  With none of those, a probed
                # test is selected unseen like the rest.
                witnesses = [n for n in light if id_of[n].startswith(tpc.EXECUTOR)] if plan.probe else []
                if plan.probe and not witnesses:
                    unprobed = unprobed | set(plan.probe)
                    plan.probe = {}
                # With a selection going out anyway, a long test often rides along
                # on a worker for less than the CPU it takes to settle or probe it.
                unseen = {n for n in light if n not in witnesses} | unprobed | plan.blind_numbers
                for n in sorted(settled | set(plan.probe), key=lambda n: seconds[n]):
                    saving = (pack_cost(unseen | {n}, millis) - pack_cost(unseen, millis)) / 1000.0
                    if n in settled:
                        cpu = plan.query[n][2]
                    else:
                        method = plan.query[n][0]
                        cpu = Q[method][2] + sum(Q[method][1][k] for k in plan.probe[n])
                        cpu += sum(cost_of[w] for w in witnesses) + maps.class_setup.get(tpc.EXECUTOR.rstrip(":"), 0.7)
                    if cpu * SPEED > SETTLE_ODDS * saving:
                        unseen.add(n)
                        unprobed = unprobed | {n}
                        settled.discard(n)
                        plan.probe.pop(n, None)
                if not plan.probe:
                    witnesses = []
                plan.blind_numbers.update(n for n in light if n not in witnesses)
                plan.blind_numbers.update(unprobed)
                plan.light = light = witnesses
                plan.heavy = set(plan.proxy) | settled | set(plan.probe)
                plan.ran_light = False
            # queries to run: all that can see the patch when that settles the
            # test, otherwise the cheapest few as a safety net for the rule below
            plan.exact = {}
            plan.probe_cpu = {}
            query_entries = []
            for n, (method, positions, total) in sorted(plan.query.items()):
                if n in plan.blind_numbers:
                    continue
                if n in settled:
                    chosen = positions
                    plan.exact[n] = True
                elif n in plan.probe:
                    chosen = plan.probe[n]
                    plan.exact[n] = False
                    plan.probe_cpu[n] = Q[method][2] + sum(Q[method][1][k] for k in chosen)
                else:
                    continue
                query_entries.append((n, runner.PROXY + "query:%s|%s" % (method, ",".join(map(str, sorted(chosen))))))
                expected = Q[method][2] + sum(Q[method][1][k] for k in chosen)
                query_budget[n] = max(10.0, 3.0 * expected + 5.0) * SLACK
            entries = [(n, id_of[n]) for n in light]
            entries += [(n, runner.PROXY + test_id) for n, test_id in sorted(plan.proxy.items())]
            entries += query_entries
            for k, module in enumerate(sorted(plan.imports)):
                plan.smoke[-1 - k] = module
                entries.append((-1 - k, runner.PROXY + "import:" + module))
            if not entries:
                continue
            root = os.path.join(TMP, "br-" + commit)
            copies = ["tests/test_sqlglotc.py"] if any(p.startswith("sqlglotc/") for p in plan.touched) else []
            overlay.remove(root)
            overlay.build(REPO, root, plan.files, copies)
            budgets = {n: max(4.0, 6.0 * max(estimate[n], seconds[n]) + 2.0) * SLACK for n in light}
            for n in plan.proxy:
                budgets[n] = 25.0
            for n, _entry in query_entries:
                budgets[n] = query_budget[n]
            short = not ({"tests/test_executor.py", "tests/helpers.py"} & plan.touched)
            job = runner.Job(commit, root, entries, budgets, short_tables=short, thin=thin, duck=DUCK if duck_ok else None)
            job.estimate = cpu_cost
            jobs.append(job)
            job_of[commit] = job
            for n in light:
                preload.update(maps.preload.get(id_of[n].split("::", 1)[0], ()))

        log("analysis done at %.2f s; %d of %d commits need a run" % (time.monotonic() - START, len(jobs), len(commits)))

        if jobs:
            for name in sorted(preload | {"unittest", "unittest.mock"}):
                try:
                    __import__(name)
                except Exception:
                    pass
            # longest first, so the two lanes stay balanced
            jobs.sort(key=lambda j: -j.estimate)
            runner.run_jobs(jobs, parallel=PARALLEL, stop_at=stop_at, log=log, hurry_at=hurry_at)
            for job in jobs:
                overlay.remove(job.root)

        for commit in commits:
            plan = plans[commit]
            if plan.blind_all:
                log(commit, "selecting everything:", "; ".join(plan.notes))
                answer(commit, everything)
                continue
            selected = set(plan.blind_numbers)
            job = job_of.get(commit)
            light_failed = set()
            unfinished = False
            if job is not None:
                unfinished = bool(job.pending)
                light_failed = {n for n in job.failed if n >= 0 and n not in plan.proxy and n not in plan.query}
                # a test that is here only because its fixture file changed says
                # nothing about what the code change does to other tests
                fixture_only = {number_of[maps.ids[i]] for i in bits(plan.cands & ~plan.other_mask) if maps.ids[i] in number_of}
                light_failed -= fixture_only
                selected |= {n for n in job.failed if n >= 0}        # failed tests and stand-ins
                selected |= {n for n, _ in job.pending if n >= 0}    # never got to run
                for k, module in plan.smoke.items():
                    if k in job.failed or any(n == k for n, _ in job.pending):
                        # the module no longer imports: everything that loads it fails
                        mask = plan.loaders.get(module, maps.all)
                        selected |= {number_of[maps.ids[i]] for i in bits(mask) if maps.ids[i] in number_of}
                        light_failed.add(k)
            # The long TPC tests.  When every query that can see the patch was
            # run, that run is the answer.  Otherwise they are selected when a
            # run query fails, when a cheaper test reaching the same change
            # fails, when too few cheaper tests reach it to say anything, or
            # when their own inputs changed.
            evidence = (
                bool(light_failed)
                or unfinished
                or len(plan.light) < MIN_EVIDENCE
                or any(p.startswith(EXECUTOR_PATHS) for p in plan.touched)
            )
            executor_failed = any(id_of[n].startswith(tpc.EXECUTOR) for n in light_failed if n >= 0)
            strong = (
                len(light_failed) >= STRONG_EVIDENCE
                or executor_failed
                or unfinished
                or any(p.startswith(EXECUTOR_PATHS) for p in plan.touched)
            )
            for n in plan.heavy:
                if n in plan.proxy or n in selected:
                    continue
                exact = getattr(plan, "exact", {}).get(n)
                if exact:
                    continue  # ran in full and passed (a failure is already selected)
                if exact is False:
                    # probed and clean: left out unless many cheaper tests fail,
                    # or the probe itself ran far slower than it should
                    slow = job is not None and job.elapsed.get(n, 0.0) > (2.0 * plan.probe_cpu.get(n, 0.0) + 2.0) * SLACK
                    if strong or slow:
                        selected.add(n)
                elif evidence:
                    selected.add(n)
            if VERBOSE:
                where = {}
                for n in light_failed:
                    if n >= 0:
                        key = id_of[n].split("::")[0].rsplit("/", 1)[-1]
                        where[key] = where.get(key, 0) + 1
                log(commit, "evidence", json.dumps({"failed_in": where, "mode": "run" if getattr(plan, "ran_light", False) else "blind",
                    "probe": {id_of[n].rsplit("::", 1)[-1]: ("fail" if job is not None and n in job.failed else "pass") for n in getattr(plan, "probe", {})}}))
                log(commit, "cands=%d ran=%d failed=%d selected=%d cpu=%.2f rec=%.2f est=%.2f" % (len(plan.numbers), len(plan.light), len(light_failed), len(selected), job.cpu if job else 0, sum(seconds[n] for n in plan.light), job.estimate if job else 0), "|", "; ".join(plan.notes))
                if job and os.environ.get("BR_TRACE"):
                    log(commit, "failed:", ", ".join(sorted(id_of.get(n, str(n)).split("::")[-1] for n in light_failed))[:600])
                    top = sorted(job.elapsed.items(), key=lambda kv: -kv[1])[:6]
                    log(commit, "trace: in tests %.2f s of %.2f s cpu;" % (sum(job.elapsed.values()), job.cpu), ", ".join("%s %.2f" % (id_of.get(n, str(n)).split("::")[-1], e) for n, e in top))
            answer(commit, selected)
    except Exception as error:
        import traceback

        traceback.print_exc(file=sys.stderr)
        log("falling back to selecting everything for unanswered commits:", repr(error))

    lines = []
    for commit in commits:
        workers = answered.get(commit)
        if workers is None:
            workers = pack(everything, millis)
        lines.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))
    payload = ("\n".join(lines) + "\n").encode() if lines else b""
    while payload:
        written = os.write(out, payload)
        payload = payload[written:]
    log("done in %.2f s wall, %.2f s cpu (self+children)" % (time.monotonic() - START, sum(os.times()[:4])))


if __name__ == "__main__":
    real_stdout = os.dup(1)
    import runner as _runner

    _runner.CLOSE_IN_CHILD.append(real_stdout)
    os.dup2(2, 1)  # stray prints from anything we run go to stderr, never to the answers
    main(real_stdout)
