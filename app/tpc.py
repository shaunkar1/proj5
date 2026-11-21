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
    EXECUTOR + "test_execute_tpcds": ("tests/test_executor.py", "tests/helpers.py", "tests/fixtures/optimizer/tpc-"),
    EXECUTOR + "test_execute_tpch": ("tests/test_executor.py", "tests/helpers.py", "tests/fixtures/optimizer/tpc-"),
    EXECUTOR + "test_optimized_tpch": ("tests/test_executor.py", "tests/helpers.py", "tests/fixtures/optimizer/tpc-"),
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


def size(test_id, module):
    """How many positions the test loops over at the pinned commit."""
    _mod, _cls, _method, attr, _schema = TARGETS[test_id]
    if attr is None:
        return len(list(module.Dialects))
    return len(list(module.load_sql_fixture_pairs(FIXTURES[attr])))


def thin_executor(module, selection):
    """selection: test id -> set of query positions (0-based) to keep.
    Must be called before the class fixture runs."""
    cls = module.TestExecutor
    keep = {}
    needs_rows = {}
    for test_id, positions in selection.items():
        _mod, _cls, _method, attr, schema = TARGETS[test_id]
        keep.setdefault(attr, set()).update(positions)
        if schema:
            needs_rows[attr] = schema
    shim = _SomeTablesCsv(getattr(module.csv, "_real", module.csv), set())
    module.csv = shim
    original = cls.setUpClass.__func__

    def setUpClass(klass):
        # which tables do the kept queries mention?  Decided from the fixture
        # text, before the tables are read.
        wanted = set()
        for attr, schema_name in needs_rows.items():
            pairs = list(module.load_sql_fixture_pairs(FIXTURES[attr]))
            schema = getattr(module, schema_name)
            for position in keep.get(attr, ()):
                if position < len(pairs):
                    sql = pairs[position][1].lower()
                    for table in schema:
                        if re.search(r"\b%s\b" % re.escape(table.lower()), sql):
                            wanted.add(table)
        shim.full = wanted
        original(klass)
        for attr, positions in keep.items():
            pairs = getattr(klass, attr)
            setattr(klass, attr, [pair if i in positions else ({}, "SELECT 1", "SELECT 1") for i, pair in enumerate(pairs)])

    cls.setUpClass = classmethod(setUpClass)


def thin_generator(module, positions):
    module.Dialects = [name for i, name in enumerate(module.Dialects) if i in positions]


def thin(modules, selection):
    """modules: module name -> imported module; selection: test id -> positions."""
    executor = {t: p for t, p in selection.items() if TARGETS[t][0] == "tests.test_executor"}
    if executor:
        thin_executor(modules["tests.test_executor"], executor)
    if GENERATOR_MEMORY in selection:
        thin_generator(modules["tests.test_generator"], selection[GENERATOR_MEMORY])
