"""Record which lines run while every sqlglot module is imported (pinned commit, offline)."""
import importlib, json, os, pkgutil, sys
import coverage

ROOT = os.getcwd()
OUT = os.environ["MAP_OUT"]
sys.path.insert(0, ROOT)
cov = coverage.Coverage(data_file=None, source=[os.path.join(ROOT, "sqlglot")], config_file=False)
started = set()
instantiated = set()
mon = sys.monitoring
mon.use_tool_id(4, "started")
def on_start(code, offset):
    f = code.co_filename
    if f.startswith(ROOT + os.sep) and code.co_name != "<module>":
        started.add((os.path.relpath(f, ROOT), code.co_firstlineno, code.co_name))
    if code.co_name != "__init__" or not code.co_varnames:
        return mon.DISABLE
    try:
        t = type(sys._getframe(1).f_locals.get(code.co_varnames[0]))
        for k in t.__mro__:
            if k.__module__.startswith("sqlglot"):
                instantiated.add(k.__module__ + ":" + k.__qualname__)
    except Exception:
        pass
mon.register_callback(4, mon.events.PY_START, on_start)
mon.set_events(4, mon.events.PY_START)
cov.start()
import sqlglot
base = sorted(m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot."))
for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if info.name.endswith("__main__"):
        continue
    importlib.import_module(info.name)
cov.stop()
mon.set_events(4, 0)
data = cov.get_data()
lines = {os.path.relpath(f, ROOT): sorted(data.lines(f) or []) for f in data.measured_files()}
# class -> module-qualified names of every sqlglot class, with its bases, for subclass lookups
classes = {}
for name, mod in list(sys.modules.items()):
    if not (name == "sqlglot" or name.startswith("sqlglot.")) or mod is None:
        continue
    stack = [v for v in vars(mod).values() if isinstance(v, type) and v.__module__ == name]
    seen = set()
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        classes[c.__module__ + ":" + c.__qualname__] = [b.__module__ + ":" + b.__qualname__ for b in c.__mro__[1:] if b.__module__.startswith("sqlglot")]
        stack.extend(v for v in vars(c).values() if isinstance(v, type) and v.__module__ == name)
# A dialect class reaches its tokenizer, parser and generator classes as plain
# attributes, without making an instance of them.  For every class: the dialect
# classes that hold it, or a subclass of it, that way.
from sqlglot.dialects.dialect import Dialect as _Dialect
def _key(c):
    return c.__module__ + ":" + c.__qualname__
def _all_subclasses(c, seen=None):
    seen = seen if seen is not None else set()
    for sub in c.__subclasses__():
        if sub not in seen:
            seen.add(sub)
            _all_subclasses(sub, seen)
    return seen
holders = {}
for dialect in [_Dialect] + sorted(_all_subclasses(_Dialect), key=_key):
    for attr in dir(dialect):
        if attr.startswith("__"):
            continue
        try:
            value = getattr(dialect, attr)
        except Exception:
            continue
        if isinstance(value, type) and value.__module__.startswith("sqlglot") and not issubclass(value, _Dialect):
            for ancestor in value.__mro__:
                if ancestor.__module__.startswith("sqlglot"):
                    holders.setdefault(_key(ancestor), set()).add(_key(dialect))
print(len(holders), "classes held by dialect classes")
json.dump({"holders": {k: sorted(v) for k, v in holders.items()}, "instantiated": sorted(instantiated), "started": sorted(started), "import_lines": lines, "base_modules": base, "classes": classes}, open(os.path.join(OUT, "importpass.json"), "w"))
print(len(lines), "files", sum(map(len, lines.values())), "import-time lines", len(base), "base modules", len(classes), "classes")
