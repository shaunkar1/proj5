# Blast Radius selector

Entry for the RWX Blast Radius track: given a patch against sqlglot at
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, pick the tests that can fail and split them over workers.

## How it works

1. **Reach.** `maps/map.bin` is built once from the pinned commit (`tools/`). Every test is run alone
   and the lines it executes, the functions it enters, the files it opens and the sqlglot classes it
   instantiates are recorded. A patch is located in the old source by comparing syntax trees
   (`app/astdiff.py`), and the tests whose own execution reaches the changed code become candidates
   (`app/analyze.py`). A patch that leaves the program unchanged (comments, formatting) has none.
2. **Run.** The cheap candidates are run against the patched tree inside the container
   (`app/runner.py`), in a forked child that already has the third-party modules loaded. Only tests
   that fail are selected. When running would cost more CPU than selecting the candidates unseen,
   they are selected unseen.
3. **Slow tests.** The two slowest single tests get a scaled-down run of the same checks
   (`app/proxies.py`). The long TPC executor tests are never run; they are selected when a cheaper
   test that reaches the same change fails.
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
