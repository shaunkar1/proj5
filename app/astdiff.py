"""Locate where two versions of a Python file differ, in terms of the old file.

Each difference is reported as a "site":

  ("lines", lo, hi, first)  code that runs when a function is called; lo..hi are
                            old-file lines of the smallest statement (or statement
                            header) holding the change, `first` the function's
                            first line (decorators included)
  ("func", lo, hi, first)   same, but the change reshapes the function, so its
                            whole span lo..hi is reported
  ("class", name, lo, hi, targets)
                            code that runs at import time inside a class body;
                            targets are the names it assigns, when it is a plain
                            assignment, else None
  ("module", lo, hi, targets)
                            code that runs at import time at module level
  ("enter", first)          a default value or annotation of a function defined at
                            class or module level: seen by calls of that function

An empty list means the two files compile to the same program.
"""

import ast

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_BLOCKS = (ast.stmt, ast.ExceptHandler, ast.match_case)


_LINES = [None, None]  # source lines of the old and the new file being compared


def _text(node, lines):
    """Source text of a node, or None when it has no position."""
    lo = getattr(node, "lineno", None)
    hi = getattr(node, "end_lineno", None)
    if lo is None or hi is None or lines is None:
        return None
    for dec in getattr(node, "decorator_list", ()):
        lo = min(lo, dec.lineno)
    if lo == hi:
        return lines[lo - 1][node.col_offset : node.end_col_offset] if not getattr(node, "decorator_list", None) else lines[lo - 1]
    chunk = lines[lo - 1 : hi]
    return (chunk[0][node.col_offset :] if not getattr(node, "decorator_list", None) else chunk[0], chunk[1:-1], chunk[-1][: node.end_col_offset])


def _same(a, b):
    if type(a) is not type(b):
        return False
    ta = _text(a, _LINES[0])
    if ta is not None and ta == _text(b, _LINES[1]):
        return True  # same kind of node from the same text
    return ast.dump(a) == ast.dump(b)


def _first_line(node):
    line = getattr(node, "lineno", None)
    for dec in getattr(node, "decorator_list", ()):
        line = min(line, dec.lineno)
    return line


def _span(node):
    lo = _first_line(node)
    hi = getattr(node, "end_lineno", lo)
    return lo, hi


def _header_span(node):
    """Lines of a statement, without the statements nested in it."""
    lo = _first_line(node)
    if lo is None:  # match_case has no position of its own
        lo = node.pattern.lineno
    hi = getattr(node, "end_lineno", None)
    body = getattr(node, "body", None)
    if isinstance(body, list) and body and hasattr(body[0], "lineno"):
        hi = max(lo, body[0].lineno - 1)
    if hi is None:
        hi = lo
    return lo, hi


def _site(path, structural):
    """path: list of (node, field) from the module down to the differing node."""
    func_at = None
    for i, (node, field) in enumerate(path):
        if isinstance(node, _FUNCS) and field == "body":
            func_at = i
    if func_at is not None:
        func = path[func_at][0]
        first = _first_line(func)
        flo, fhi = _span(func)
        if structural:
            return ("func", flo, fhi, first)
        inner = None
        for node, _field in path[func_at + 1 :]:
            if isinstance(node, _BLOCKS):
                inner = node
        if inner is None:
            if isinstance(func, ast.Lambda):
                return ("lines", func.body.lineno, func.body.end_lineno, first)
            return ("func", flo, fhi, first)
        lo, hi = _header_span(inner)
        return ("lines", lo, hi, first)
    signature_of = None
    for node, field in path:
        if isinstance(node, _FUNCS) and field in ("args", "returns"):
            signature_of = node
        elif isinstance(node, (ast.ClassDef,) + _FUNCS):
            signature_of = None
    if signature_of is not None:
        return ("enter", _first_line(signature_of))
    cls = None
    names = []
    statement = None
    for i, (node, field) in enumerate(path):
        if isinstance(node, ast.ClassDef):
            cls = node
            names.append(node.name)
            statement = path[i + 1][0] if field == "body" and i + 1 < len(path) else None
    if cls is not None:
        lo, hi = _span(cls)
        return ("class", ".".join(names), lo, hi, assigned_names(statement))
    top = path[1][0] if len(path) > 1 else path[0][0]
    lo, hi = _span(top) if hasattr(top, "lineno") else (1, 1)
    return ("module", lo, hi, assigned_names(top))


def assigned_names(statement):
    """Names bound by a plain assignment statement, or None for anything else."""
    if isinstance(statement, ast.Assign):
        targets = statement.targets
    elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
        targets = [statement.target]
    else:
        return None
    if all(isinstance(t, ast.Name) for t in targets):
        return tuple(t.id for t in targets)
    return None


def _walk(a, b, path, out):
    if type(a) is not type(b):
        out.append(_site(path + [(a, None)], isinstance(a, _BLOCKS)))
        return
    for field in a._fields:
        va = getattr(a, field, None)
        vb = getattr(b, field, None)
        if isinstance(va, list) or isinstance(vb, list):
            va = va or []
            vb = vb or []
            if len(va) != len(vb):
                structural = any(isinstance(x, _BLOCKS) for x in va + vb)
                out.append(_site(path + [(a, field)], structural))
                continue
            for xa, xb in zip(va, vb):
                if isinstance(xa, ast.AST) and isinstance(xb, ast.AST):
                    if not _same(xa, xb):
                        _walk(xa, xb, path + [(a, field)], out)
                elif xa != xb:
                    out.append(_site(path + [(a, field)], False))
        elif isinstance(va, ast.AST) and isinstance(vb, ast.AST):
            if not _same(va, vb):
                _walk(va, vb, path + [(a, field)], out)
        elif isinstance(va, ast.AST) or isinstance(vb, ast.AST):
            out.append(_site(path + [(a, field)], False))
        elif va != vb or type(va) is not type(vb):
            out.append(_site(path + [(a, field)], False))


def diff(old_source, new_source):
    """Sites where the two sources differ.  Raises SyntaxError when the new
    source does not parse (ValueError for null bytes is turned into one)."""
    old_tree = ast.parse(old_source)
    try:
        new_tree = ast.parse(new_source)
    except ValueError as error:
        raise SyntaxError(str(error))

    def lines_of(source):
        if isinstance(source, bytes):
            # positions in the tree count UTF-8 bytes within a line
            return source.splitlines()
        return [line.encode("utf-8") for line in source.splitlines()]

    try:
        _LINES[0], _LINES[1] = lines_of(old_source), lines_of(new_source)
    except Exception:
        _LINES[0] = _LINES[1] = None
    out = []
    try:
        _walk(old_tree, new_tree, [], out)
    finally:
        _LINES[0] = _LINES[1] = None
    seen = []
    for site in out:
        if site not in seen:
            seen.append(site)
    return seen


def functions(source):
    """(first_line, last_line, name) of every def and lambda in a source."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, _FUNCS):
            lo, hi = _span(node)
            found.append((lo, hi, getattr(node, "name", "<lambda>")))
    return found
