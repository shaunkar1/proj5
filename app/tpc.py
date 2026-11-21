"""Run a looping test on a chosen part of what it loops over.

A few sqlglot tests are one long loop: test_execute_tpcds, test_execute_tpch and
test_optimized_tpch go over the TPC queries that the class fixture loads, and
test_generator_memory goes over every dialect.  The real test method is run
unchanged; only the list it loops over is thinned.  For the TPC tests the
queries outside the chosen part become trivial ones and only the tables the
chosen queries read are loaded in full.
"""

import itertools
import re

EXECUTOR = "tests/test_executor.py::TestExecutor::"
GENERATOR_MEMORY = "tests/test_generator.py::TestGenerator::test_generator_memory"

# test id -> (module, class, method, fixture list on the class, schema name in the module)
TARGETS = {
    EXECUTOR + "test_execute_tpcds": ("tests.test_executor", "TestExecutor", "test_execute_tpcds", "tpcds_sqls", "TPCDS_SCHEMA"),
    EXECUTOR + "test_execute_tpch": ("tests.test_executor", "TestExecutor", "test_execute_tpch", "tpch_sqls", "TPCH_SCHEMA"),
    EXECUTOR + "test_optimized_tpch": ("tests.test_executor", "TestExecutor", "test_optimized_tpch", "tpch_sqls", None),
    GENERATOR_MEMORY: ("tests.test_generator", "TestGenerator", "test_generator_memory", None, None),
}
# a patch touching any of these changes what the positions mean
UNSTABLE = {
    EXECUTOR + "test_execute_tpcds": ("tests/test_executor.py", "tests/helpers.py", "tests/fixtures/optimizer/tpc-ds/"),
    EXECUTOR + "test_execute_tpch": ("tests/test_executor.py", "tests/helpers.py", "tests/fixtures/optimizer/tpc-h/"),
    EXECUTOR + "test_optimized_tpch": ("tests/test_executor.py", "tests/helpers.py", "tests/fixtures/optimizer/tpc-h/"),
    GENERATOR_MEMORY: ("tests/test_generator.py", "sqlglot/dialects/__init__.py", "sqlglot/dialects/dialect.py"),
}
FIXTURES = {"tpcds_sqls": "optimizer/tpc-ds/tpc-ds.sql", "tpch_sqls": "optimizer/tpc-h/tpc-h.sql"}


class _SomeTablesCsv:
    """csv stand-in: full rows for the tables named in `full`, header and one row for the rest."""

    def __init__(self, real, full):
        self._real = real
        self.full = full

    def __getattr__(self, name):
        return getattr(self._real, name)

    def reader(self, source, *args, **kwargs):
        rows = self._real.reader(source, *args, **kwargs)
        name = str(getattr(source, "name", ""))
        table = name.rsplit("/", 1)[-1].split(".", 1)[0]
        if table in self.full:
            return rows
        return itertools.islice(rows, 2)


def fixture_pairs(text):
    """The (sql, expected) texts tests/helpers.py's load_sql_fixture_pairs yields for a fixture."""
    kept = "\n".join(line for line in text.splitlines() if line and not line.startswith("--"))
    statements = kept.split(";")
    return [(statements[i].strip(), statements[i + 1].strip()) for i in range(0, len(statements) - 1, 2)]


def changed_positions(pinned_text, patched_text):
    """Positions whose pair differs between two versions of a fixture, or None
    when the pairs no longer line up."""
    old, new = fixture_pairs(pinned_text), fixture_pairs(patched_text)
    if len(old) != len(new):
        return None
    return {i for i, (a, b) in enumerate(zip(old, new)) if a != b}


def size(test_id, module):
    """How many positions the test loops over at the pinned commit."""
    _mod, _cls, _method, attr, _schema = TARGETS[test_id]
    if attr is None:
        return len(list(module.Dialects))
    return len(list(module.load_sql_fixture_pairs(FIXTURES[attr])))


class _Late:
    """Class attribute holding a query list that is chosen when the test first
    asks for it: the short list, or the full one if `widen()` says so."""

    def __init__(self, short, widen):
        self._short = short
        self._widen = widen
        self._value = None

    def __get__(self, instance, owner):
        if self._value is None:
            wider = self._widen()
            self._value = self._short if wider is None else wider
        return self._value


def thin_executor(module, selection, wider=None):
    """selection: test id -> set of query positions (0-based) to keep.
    wider: test id -> (larger set of positions, should(), done()): when the
    test starts and should() is true, it runs the larger set instead, and
    done() is called.
    Must be called before the class fixture runs."""
    cls = module.TestExecutor
    wider = wider or {}
    keep = {}
    needs_rows = {}
    for test_id, positions in selection.items():
        _mod, _cls, _method, attr, schema = TARGETS[test_id]
        keep.setdefault(attr, set()).update(positions)
        if schema:
            needs_rows[attr] = schema
    late = {}
    for test_id, (positions, should, done) in wider.items():
        attr = TARGETS[test_id][3]
        if sum(1 for t in selection if TARGETS[t][3] == attr) == 1:  # no other test shares the list
            late[attr] = (set(positions), should, done)
    shim = _SomeTablesCsv(getattr(module.csv, "_real", module.csv), set())
    module.csv = shim
    original = cls.setUpClass.__func__

    def tables_of(kept):
        # which tables do the kept queries mention?  Decided from the fixture
        # text, before the tables are read.
        wanted = set()
        for attr, schema_name in needs_rows.items():
            pairs = list(module.load_sql_fixture_pairs(FIXTURES[attr]))
            schema = getattr(module, schema_name)
            for position in kept.get(attr, ()):
                if position < len(pairs):
                    sql = pairs[position][1].lower()
                    for table in schema:
                        if re.search(r"\b%s\b" % re.escape(table.lower()), sql):
                            wanted.add(table)
        return wanted

    def thinned(pairs, positions):
        return [pair if i in positions else ({}, "SELECT 1", "SELECT 1") for i, pair in enumerate(pairs)]

    def setUpClass(klass):
        shim.full = tables_of(keep)
        original(klass)
        for attr, positions in keep.items():
            short = thinned(getattr(klass, attr), positions)
            if attr not in late:
                setattr(klass, attr, short)
                continue

            def widen(attr=attr):
                more, should, done = late[attr]
                if not should():
                    return None
                # Load again with the tables the larger set needs.  Objects the
                # test may already hold (the table dicts) are refilled in place.
                held = {name: value for name, value in vars(klass).items() if isinstance(value, dict)}
                shim.full = tables_of(dict(keep, **{attr: more}))
                original(klass)
                for name, old in held.items():
                    fresh = vars(klass).get(name)
                    if isinstance(fresh, dict) and fresh is not old:
                        old.clear()
                        old.update(fresh)
                        setattr(klass, name, old)
                for other, kept in keep.items():
                    if other != attr:
                        setattr(klass, other, thinned(getattr(klass, other), kept))
                value = thinned(getattr(klass, attr), more)
                setattr(klass, attr, value)
                done()
                return value

            setattr(klass, attr, _Late(short, widen))

    cls.setUpClass = classmethod(setUpClass)


def thin_generator(module, positions):
    module.Dialects = [name for i, name in enumerate(module.Dialects) if i in positions]


def thin(modules, selection, wider=None):
    """modules: module name -> imported module; selection: test id -> positions."""
    executor = {t: p for t, p in selection.items() if TARGETS[t][0] == "tests.test_executor"}
    if executor:
        thin_executor(modules["tests.test_executor"], executor, wider)
    if GENERATOR_MEMORY in selection:
        thin_generator(modules["tests.test_generator"], selection[GENERATOR_MEMORY])
