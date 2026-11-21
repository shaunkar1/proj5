"""Run the long TPC tests of tests/test_executor.py on a chosen subset of queries.

test_execute_tpcds, test_execute_tpch and test_optimized_tpch each loop over a
list of queries that the class fixture loads from the TPC fixture files.  The
real test method is run unchanged; only the fixture list is thinned so that the
queries outside the subset become trivial ones, and only the tables the subset
reads are loaded in full.
"""

import itertools
import re

MODULE = "tests.test_executor"
CLASS = "TestExecutor"
# test method -> (fixture list on the class, schema name in the test module)
TESTS = {
    "test_execute_tpcds": ("tpcds_sqls", "TPCDS_SCHEMA"),
    "test_execute_tpch": ("tpch_sqls", "TPCH_SCHEMA"),
    "test_optimized_tpch": ("tpch_sqls", None),
}
PREFIX = "tests/test_executor.py::TestExecutor::"
IDS = {PREFIX + name: name for name in TESTS}
# a patch touching any of these changes what the query numbers mean
UNSTABLE = ("tests/test_executor.py", "tests/helpers.py", "tests/fixtures/optimizer/tpc-")


class _SomeTablesCsv:
    """csv stand-in: full rows for the tables named in `full`, header and one row for the rest."""

    def __init__(self, real, full):
        self._real = real
        self.full = full  # None: everything

    def __getattr__(self, name):
        return getattr(self._real, name)

    def reader(self, source, *args, **kwargs):
        rows = self._real.reader(source, *args, **kwargs)
        if self.full is None:
            return rows
        name = str(getattr(source, "name", ""))
        table = name.rsplit("/", 1)[-1].split(".", 1)[0]
        if table in self.full:
            return rows
        return itertools.islice(rows, 2)


def thin(module, selection):
    """selection: test method -> set of query positions (0-based) to keep.
    Must be called before the class fixture runs."""
    cls = getattr(module, CLASS)
    keep = {}
    for method, positions in selection.items():
        attr, _schema = TESTS[method]
        keep.setdefault(attr, set()).update(positions)
    needs_rows = {TESTS[m][0]: TESTS[m][1] for m in selection if TESTS[m][1]}
    shim = _SomeTablesCsv(getattr(module.csv, "_real", module.csv), set())
    module.csv = shim
    original = cls.setUpClass.__func__

    def setUpClass(klass):
        # which tables do the kept queries mention?  Decided from the fixture
        # text before the tables are read.
        wanted = set()
        for attr, schema_name in needs_rows.items():
            fixture = "optimizer/tpc-ds/tpc-ds.sql" if attr == "tpcds_sqls" else "optimizer/tpc-h/tpc-h.sql"
            pairs = list(module.load_sql_fixture_pairs(fixture))
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
