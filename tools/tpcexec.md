maps/fastcpu.json: CPU seconds of each TPC query inside the long executor tests
when DuckDB's answers come from the cache made by build/duckcache.py, measured
at the pinned commit in one run of the whole test (engine time per query; the
base is the class fixture plus first-use warm-up).  Used only to estimate what
a run will cost.
