# Blast Radius selector

Entry for the RWX Blast Radius track: given a patch against sqlglot at
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, pick the tests that can fail and split them over workers.

## How it works

1. **Reach.** `maps/map.bin` is built once from the pinned commit (`tools/`). Every test is run alone
   and the lines it executes, the functions it enters, the files it opens and the sqlglot classes it
   instantiates are recorded. A patch is located in the old source by comparing syntax trees
   (`app/astdiff.py`), and the tests whose own execution reaches the changed code become candidates
   (`app/analyze.py`). A patch that leaves the program unchanged (comments, formatting, documentation
   strings: the doctest loader is not among the listed tests) has none. A changed class-level
   setting is narrowed to the tests that use the class and reach a statement reading that name.
2. **Run.** The cheap candidates are run against the patched tree inside the container
   (`app/runner.py`), in a forked child that already has the third-party modules loaded. Only tests
   that fail are selected; a test that hangs or kills its child counts as failing. When running
   would cost more CPU than selecting the candidates unseen, they are selected unseen.
3. **Long tests.** A few tests are one long loop. `test_execute_tpcds`, `test_execute_tpch` and
   `test_optimized_tpch` go over the TPC queries, `test_generator_memory` over the dialects, and
   several optimizer tests over the entries of a fixture file. The map also records what each
   query, dialect or entry reaches on its own, so the real test method is run on just the part a
   patch can affect (`app/tpc.py`, `app/thinload.py`). When that part is too large to run, the
   cheapest ten queries are run as a probe and the test is selected if the probe or a cheaper
   executor test that reaches the change fails. `test_max_nodes` gets a scaled-down run of the same
   check (`app/proxies.py`), only for patches confined to `sqlglot/`.
   The executor tests compare sqlglot's engine with DuckDB. DuckDB's side does not depend on
   sqlglot once the SQL text is fixed, so the image build records its answers for the pinned
   fixtures (`build/duckcache.py`), keyed by the exact SQL sent; a patched tree that sends the same
   text gets the recorded answer and any other text goes to DuckDB (`app/duckmemo.py`). This is
   only used when a patch leaves tests and fixtures alone.
4. **Pack.** Selected tests go longest-first onto the lightest worker, for the worker count that
   minimises busiest load plus 2 s per worker.

Anything unusual (a patch that does not apply, a renamed or new module, test configuration) falls
back to selecting more tests, never fewer.

## Layout

- `Dockerfile` installs the labeled package set, fetches sqlglot at the pinned commit and compiles it.
- `app/` is the selector. `maps/` holds the committed map. `tools/` rebuilds the map from a checkout.
- `make sample` (with the starter kit's `sample/` and `score.py` beside it) builds, runs and scores.

## Rebuilding the map

```sh
python tools/importpass.py                # in the sqlglot checkout, MAP_OUT=<dir>
python tools/isomap.py <checkout> <tests.json> <iso dir>
python tools/timepass.py <checkout> <tests.json> <times.json>
python tools/packmaps.py <iso dir> <dir>/importpass.json <checkout> maps/map.bin <times.json>
```
