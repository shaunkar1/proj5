"""Record what DuckDB answers for the TPC queries of the pinned commit.

tests/test_executor.py checks sqlglot's own engine against DuckDB: it sends
each TPC query (transpiled to DuckDB's dialect) to DuckDB over the fixture
tables and compares.  DuckDB's part of that does not depend on sqlglot once
the SQL text is fixed, so its answers are kept here, keyed by the exact SQL
text sent.  At run time a patched tree that sends the same text gets the
recorded answer; any other text goes to DuckDB as before.

usage: duckcache.py <sqlglot checkout> <output file>
"""

import os
import pickle
import sys
import time


def main():
    repo, out = os.path.abspath(sys.argv[1]), sys.argv[2]
    sys.path.insert(0, repo)
    os.chdir(repo)
    os.environ["SKIP_INTEGRATION"] = "0"
    import duckdb

    from sqlglot import transpile
    from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    def connect(directory, schema):
        conn = duckdb.connect(config={"threads": 2})
        for table, columns in schema.items():
            file_name = f"{directory}{table}.csv.gz"
            conn.execute(
                f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})
                """
            )
        return conn

    frames = {}
    seconds = {"tpcds_sqls": [], "tpch_sqls": [], "tpch_optimized": []}

    def ask(conn, sql):
        started = time.process_time()
        if sql not in frames:
            frames[sql] = conn.execute(sql).fetchdf()
        return time.process_time() - started

    conn = connect(FIXTURES_DIR + "/optimizer/tpc-h/", TPCH_SCHEMA)
    for _meta, sql, optimized in load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"):
        seconds["tpch_sqls"].append(round(ask(conn, transpile(sql, write="duckdb")[0]), 4))
        seconds["tpch_optimized"].append(round(ask(conn, transpile(optimized, write="duckdb")[0]), 4))
    conn.close()

    conn = connect(FIXTURES_DIR + "/optimizer/tpc-ds/", TPCDS_SCHEMA)
    for meta, sql, _optimized in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
        spent = 0.0
        if meta.get("execute"):
            spent = ask(conn, transpile(sql, write="duckdb")[0])
        seconds["tpcds_sqls"].append(round(spent, 4))
    conn.close()

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as handle:
        pickle.dump({"frames": frames, "seconds": seconds}, handle, protocol=pickle.HIGHEST_PROTOCOL)
    print("recorded %d answers, %.1f s of DuckDB time" % (len(frames), sum(map(sum, seconds.values()))), file=sys.stderr)


if __name__ == "__main__":
    main()
