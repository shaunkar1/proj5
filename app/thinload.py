"""Thin fixture-driven tests to the fixture entries that matter.

Many sqlglot tests loop over the entries of a fixture file, one independent
check per entry, through the two loaders in tests/helpers.py.  When all a patch
does to such a test is change its fixture file, only the entries that differ
from the pinned file can behave differently, so the loaders hand the test just
those.  When a patch changes code, the map says which entries reach that code
(by position, at the pinned commit), and the loaders hand the test those.

The test method itself always runs; only the loaders' output is filtered.
"""

import collections

# what the running test is to be given: ("changed", {fixture names}) or ("keep", {positions})
STATE = {"spec": None, "count": 0}


def begin(spec):
    STATE["spec"] = spec
    STATE["count"] = 0


def end():
    STATE["spec"] = None


def _wrap(helpers, original, pinned_fixtures):
    def loader(filename):
        spec = STATE["spec"]
        if spec is None:
            yield from original(filename)
            return
        mode, wanted = spec
        if mode == "keep":
            for item in original(filename):
                position = STATE["count"]
                STATE["count"] += 1
                if position in wanted:
                    yield item
            return
        if filename not in wanted:
            return  # an untouched fixture: its entries behave as at the pinned commit
        patched_dir = helpers.FIXTURES_DIR
        helpers.FIXTURES_DIR = pinned_fixtures
        try:
            pinned = collections.Counter(repr(item) for item in original(filename))
        finally:
            helpers.FIXTURES_DIR = patched_dir
        for item in original(filename):
            key = repr(item)
            if pinned[key] > 0:
                pinned[key] -= 1
            else:
                yield item

    return loader


def install(helpers, pinned_fixtures):
    if getattr(helpers, "_thinload", False):
        return
    for name in ("load_sql_fixtures", "load_sql_fixture_pairs"):
        setattr(helpers, name, _wrap(helpers, getattr(helpers, name), pinned_fixtures))
    helpers._thinload = True
