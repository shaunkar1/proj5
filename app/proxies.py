"""A scaled-down run of the slowest single test.

test_max_nodes parses a two-million-node query to see the node limit trip, which
takes half a minute.  This runs the same checks with smaller sizes against the
patched tree; when the small run fails, the real test is selected.
"""


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


PROXIES = {
    "tests/test_parser.py::TestParser::test_max_nodes": max_nodes,
}
