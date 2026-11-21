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
VERBOSE = os.environ.get("BR_VERBOSE") == "1"

WORKER_MS = 2000
MAX_WORKERS = 8
SECONDS_PER_COMMIT = 4.5   # the wall-clock allowance works out to this

MAX_NODES = "tests/test_parser.py::TestParser::test_max_nodes"
GENERATOR_MEMORY = "tests/test_generator.py::TestGenerator::test_generator_memory"
EXECUTOR_HEAVY = (
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch",
)
EXECUTOR_PATHS = ("tests/test_executor.py", "tests/fixtures/optimizer/tpc-")
MIN_EVIDENCE = 3
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


def bits(mask):
    out = []
    i = 0
    while mask:
        if mask & 1:
            out.append(i)
        mask >>= 1
        i += 1
    return out


def main(out):
    import analyze
    import overlay
    import proxies
    import runner

    with open(os.path.join(DATA, "tests.json")) as handle:
        tests = json.load(handle)["tests"]
    number_of = {t["id"]: t["number"] for t in tests}
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
        # CPU seconds a test is expected to take here: measured alone at the pinned
        # commit (minus process start-up), else its recorded time
        estimate = dict(seconds)
        for i, test_id in enumerate(maps.ids):
            if test_id in number_of and maps.times[i] is not None:
                estimate[number_of[test_id]] = maps.times[i]
        # tests the maps know nothing about are treated as reachable from anything
        unknown = [n for test_id, n in number_of.items() if test_id not in set(maps.ids)]
        stop_at = START + 0.62 * SECONDS_PER_COMMIT * max(len(commits), 1)

        plans = {}
        jobs = []
        job_of = {}
        preload = set()
        for commit in commits:
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
                if test_id in EXECUTOR_HEAVY:
                    heavy.add(n)
                elif test_id in proxies.PROXIES and n not in blind:
                    if test_id.split("::", 1)[0] in plan.touched:
                        plan.blind_numbers.add(n)  # the test itself changed: no stand-in
                    else:
                        heavy.add(n)
                        plan.proxy[n] = test_id
            plan.heavy = heavy
            light = sorted(cands - heavy - plan.blind_numbers)
            plan.light = light
            plan.smoke = {}
            # Running the cheap candidates costs their CPU time; selecting all of
            # them unseen costs worker time.  Take the cheaper road.
            # Running leaves nothing to select about half the time, and usually a
            # small part of the candidates otherwise.
            blind_cost = pack_cost(cands, millis) / 1000.0
            cpu_cost = 0.08 + sum(estimate[n] for n in light)
            cpu_cost += sum(maps.class_setup.get(c, 0.05) for c in {id_of[n].rsplit("::", 1)[0] for n in light})
            run_cost = cpu_cost + FAIL_ODDS * (2.0 + FAIL_SHARE * max(blind_cost - 2.0, 0.0))
            if light and run_cost > blind_cost:
                log(commit, "cheaper to select all %d candidates (%.1f s) than to run them (%.1f s)" % (len(cands), blind_cost, run_cost))
                plan.blind_numbers.update(cands)
                plan.light = light = []
                plan.proxy = {}
                plan.heavy = set()
                continue
            entries = [(n, id_of[n]) for n in light]
            entries += [(n, runner.PROXY + test_id) for n, test_id in sorted(plan.proxy.items())]
            for k, module in enumerate(sorted(plan.imports)):
                plan.smoke[-1 - k] = module
                entries.append((-1 - k, runner.PROXY + "import:" + module))
            if not entries:
                continue
            root = os.path.join(TMP, "br-" + commit)
            copies = ["tests/test_sqlglotc.py"] if any(p.startswith("sqlglotc/") for p in plan.touched) else []
            overlay.remove(root)
            overlay.build(REPO, root, plan.files, copies)
            budgets = {n: max(8.0, 8.0 * seconds[n]) for n in light}
            for n in plan.proxy:
                budgets[n] = 25.0
            short = not ({"tests/test_executor.py", "tests/helpers.py"} & plan.touched)
            job = runner.Job(commit, root, entries, budgets, short_tables=short)
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
            runner.run_jobs(jobs, parallel=2, stop_at=stop_at, log=log)
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
                light_failed = {n for n in job.failed if n >= 0 and n not in plan.proxy}
                selected |= {n for n in job.failed if n >= 0}        # failed tests and stand-ins
                selected |= {n for n, _ in job.pending if n >= 0}    # never got to run
                for k, module in plan.smoke.items():
                    if k in job.failed or any(n == k for n, _ in job.pending):
                        # the module no longer imports: everything that loads it fails
                        mask = plan.loaders.get(module, maps.all)
                        selected |= {number_of[maps.ids[i]] for i in bits(mask) if maps.ids[i] in number_of}
                        light_failed.add(k)
            # The long TPC executor tests are never run here.  They are selected
            # when a cheaper test reaching the same change fails, when too few
            # cheaper tests reach it to say anything, or when their own inputs changed.
            evidence = (
                bool(light_failed)
                or unfinished
                or len(plan.light) < MIN_EVIDENCE
                or any(p.startswith(EXECUTOR_PATHS) for p in plan.touched)
            )
            for n in plan.heavy:
                if n in plan.proxy:
                    continue
                if evidence:
                    selected.add(n)
            if VERBOSE:
                log(commit, "cands=%d ran=%d failed=%d selected=%d cpu=%.2f rec=%.2f" % (len(plan.numbers), len(plan.light), len(light_failed), len(selected), job.cpu if job else 0, sum(seconds[n] for n in plan.light)), "|", "; ".join(plan.notes))
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
