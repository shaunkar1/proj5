"""Records, for every `is` / `is not` comparison in sqlglot's source, the kinds of operands it sees."""
import enum, json, os

_seen = set()
_out = None


def _eq_owner(tp):
    for klass in tp.__mro__:
        if "__eq__" in vars(klass):
            return "%s:%s" % (klass.__module__, klass.__qualname__)
    return "?"


def _describe(x):
    tp = type(x)
    return ["%s:%s" % (tp.__module__, tp.__qualname__), _eq_owner(tp), isinstance(x, enum.Enum)]


def is_(a, b, site):
    key = (site, type(a), type(b))
    if key not in _seen:
        _seen.add(key)
        global _out
        if _out is None:
            _out = open(os.environ.get("BRPROF_OUT", "/tmp/brprof.log"), "a", buffering=1)
        try:
            _out.write(json.dumps([site] + _describe(a) + _describe(b)) + "\n")
        except Exception:
            _out.write(json.dumps([site, "?", "?", False, "?", "?", False]) + "\n")
    return a is b
