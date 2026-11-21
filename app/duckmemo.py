"""Answer DuckDB queries of tests.test_executor from the answers recorded at
build time (build/duckcache.py), by exact SQL text.

The stand-in replaces the name `duckdb` inside the test module.  A statement
whose text was recorded gets the recorded frame; anything else goes to a real
connection, opened on first need with the view definitions replayed.  Only
used when a patch leaves the test files and fixtures alone, so that what
DuckDB would see is fixed by the SQL text.
"""

import pickle


class _Answer:
    def __init__(self, frame):
        self._frame = frame

    def fetchdf(self):
        return self._frame.copy()

    df = fetchdf


class _Connection:
    def __init__(self, owner, args, kwargs):
        self._owner = owner
        self._args = args
        self._kwargs = kwargs
        self._real = None
        self._views = []

    def _open(self):
        if self._real is None:
            real = self._owner.real.connect(*self._args, **self._kwargs)
            for sql in self._views:
                real.execute(sql)
            self._views = []
            self._real = real
        return self._real

    def execute(self, sql, *args, **kwargs):
        if not args and not kwargs and isinstance(sql, str):
            frame = self._owner.frames().get(sql)
            if frame is not None:
                return _Answer(frame)
            if self._real is None and sql.lstrip()[:11].upper() == "CREATE VIEW":
                self._views.append(sql)
                return self
        return self._open().execute(sql, *args, **kwargs)

    def close(self):
        if self._real is not None:
            self._real.close()

    def __getattr__(self, name):
        return getattr(self._open(), name)


class Module:
    """Looks like the duckdb module to the test file."""

    def __init__(self, real, path):
        self.real = real
        self._path = path
        self._frames = None

    def frames(self):
        if self._frames is None:
            try:
                with open(self._path, "rb") as handle:
                    self._frames = pickle.load(handle)["frames"]
            except Exception:
                self._frames = {}
        return self._frames

    def connect(self, *args, **kwargs):
        return _Connection(self, args, kwargs)

    def __getattr__(self, name):
        return getattr(self.real, name)


def install(module, path):
    if not isinstance(module.duckdb, Module):
        module.duckdb = Module(module.duckdb, path)
