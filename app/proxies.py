"""Scaled-down runs of the two slowest single tests.

test_max_nodes parses a two-million-node query to see the node limit trip, and
test_generator_memory builds 1100 generators per dialect to look for a leak.
Both take many seconds.  These run the same checks with smaller sizes against
the patched tree; when the small run fails, the real test is selected.
"""

import gc
import tracemalloc


def max_nodes():
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    assert isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)

    cols = [f"col{i}" for i in range(500)]
    sql = f"SELECT {','.join(cols)} FROM t"
    assert isinstance(parse_one(sql, max_nodes=1_000_000), exp.Select)

    width = 120
    depth = 100
    expr = "x" + "=x" * depth
    cols = [expr for _ in range(width)]
    sql = f"SELECT {','.join(cols)}"

    for limit in (12_000, 600):
        try:
            parse_one(sql, max_nodes=limit)
        except ParseError as error:
            assert "Maximum number of AST nodes" in str(error)
            assert "(%d)" % limit in str(error)
        else:
            raise AssertionError("limit %d not enforced" % limit)

    try:
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
    except ParseError:
        pass
    else:
        raise AssertionError("limit 5 not enforced")
    assert isinstance(parse_one("SELECT 1, 2, 3"), exp.Select)


def generator_memory():
    from sqlglot.dialects.dialect import Dialect, Dialects

    warm, rounds = 20, 200
    for name in Dialects:
        dialect = Dialect.get_or_raise(name)
        for _ in range(warm):
            dialect.generator()
        gc.collect()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(rounds):
                dialect.generator()
            gc.collect()
            after = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        # the real test allows 1024 bytes over 1000 rounds
        assert after - before < 1024 * rounds // 1000, (name, after - before)


PROXIES = {
    "tests/test_parser.py::TestParser::test_max_nodes": max_nodes,
    "tests/test_generator.py::TestGenerator::test_generator_memory": generator_memory,
}
