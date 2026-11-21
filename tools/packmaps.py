"""Offline: fold the per-test records made by isomap.py and importpass.py into
the compact map the selector ships with.  Usage: packmaps.py <iso dir> <importpass.json> <sqlglot checkout> <out file>"""
import ast, glob, json, os, pickle, sys, zlib

ISO, IMPORTPASS, ROOT, OUT = sys.argv[1:5]
records = {}
for path in glob.glob(os.path.join(ISO, "*.json")):
    records[int(os.path.basename(path)[:-5])] = json.load(open(path))
n = max(records) + 1
assert len(records) == n, (len(records), n)
ids = [records[i]["id"] for i in range(n)]

lines = {}    # file -> line -> mask
started = {}  # file -> first line -> mask
opened = {}   # file -> mask
inst = {}     # class -> mask
anyline = {}  # file -> mask
bad = []
for i in range(n):
    r = records[i]
    bit = 1 << i
    if "P %d" % i not in r["status"]:
        bad.append(i)
    for f, ls in r["lines"].items():
        d = lines.setdefault(f, {})
        anyline[f] = anyline.get(f, 0) | bit
        for l in ls:
            d[l] = d.get(l, 0) | bit
    for f, l in r["started"]:
        d = started.setdefault(f, {})
        d[l] = d.get(l, 0) | bit
    for f in r["opened"]:
        opened[f] = opened.get(f, 0) | bit
    for c in r["inst"]:
        inst[c] = inst.get(c, 0) | bit

def pack(d):
    sets, index, out = [], {}, {}
    for k, m in d.items():
        if m not in index:
            index[m] = len(sets)
            sets.append(m)
        out[k] = index[m]
    return (sets, out)

files = {}
for f in set(lines) | set(started):
    files[f] = zlib.compress(pickle.dumps((pack(lines.get(f, {})), pack(started.get(f, {}))), protocol=5), 6)

ip = json.load(open(IMPORTPASS))

# third-party / stdlib modules each test file imports at module level, for preloading
preload = {}
for path in sorted(glob.glob(os.path.join(ROOT, "tests", "**", "test_*.py"), recursive=True)):
    rel = os.path.relpath(path, ROOT)
    mods = set()
    for node in ast.parse(open(path).read()).body:
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
    preload[rel] = sorted(m for m in mods if not m.startswith(("sqlglot", "tests")))

# Where class-level names are read.  For every name assigned in a class body
# somewhere in sqlglot: the functions that mention it.  A name that is also
# used while modules are being imported (outside functions, or inside functions
# that run at import time) cannot be followed, and is stored as None.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import astdiff

class_names = set()
trees = {}
for path in sorted(glob.glob(os.path.join(ROOT, "sqlglot", "**", "*.py"), recursive=True) + glob.glob(os.path.join(ROOT, "tests", "**", "*.py"), recursive=True)):
    rel = os.path.relpath(path, ROOT)
    trees[rel] = ast.parse(open(path).read())
    if rel.startswith("sqlglot/"):
        for node in ast.walk(trees[rel]):
            if isinstance(node, ast.ClassDef):
                for st in node.body:
                    class_names.update(astdiff.assigned_names(st) or ())
import_started = {(f, l) for f, l, _name in ip["started"]}
readers = {name: set() for name in class_names}
edges = {name: set() for name in class_names}
blocked = set()

def visit(rel, node, func, class_targets, in_class_body):
    for field, value in ast.iter_fields(node):
        children = value if isinstance(value, list) else [value]
        for child in children:
            if not isinstance(child, ast.AST):
                continue
            c_func, c_targets, c_body = func, class_targets, False
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and field == "body":
                c_func, c_targets = astdiff._first_line(node), None
            elif isinstance(node, ast.ClassDef) and field == "body":
                c_func, c_targets, c_body = (func, astdiff.assigned_names(child) or (), True) if func is None else (func, None, False)
            elif func is None and in_class_body is False and isinstance(node, ast.Module):
                c_targets = None
            name = None
            if isinstance(child, ast.Attribute):
                name = child.attr
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                name = child.id
            elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                name = child.value
            if name in readers:
                if c_func is not None:
                    readers[name].add((rel, c_func))
                elif c_targets:
                    edges[name].update(c_targets)
                else:
                    blocked.add(name)
            visit(rel, child, c_func, c_targets, c_body)

for rel, tree in trees.items():
    visit(rel, tree, None, None, False)
refs = {}
for name in class_names:
    if name in blocked or len(readers[name]) > 300 or any(r in import_started for r in readers[name]):
        refs[name] = None
    else:
        refs[name] = (sorted(readers[name]), sorted(edges[name] - {name}))
print("class-level names", len(class_names), "followable", sum(v is not None for v in refs.values()))

timing = json.load(open(sys.argv[5])) if len(sys.argv) > 5 else {"tests": {}, "classes": {}}
times = timing["tests"]

index = {
    "refs": refs,
    "times": [times.get(ids[i]) for i in range(n)],
    "class_setup": timing["classes"],
    "ids": ids,
    "cpu": [records[i]["cpu"] for i in range(n)],
    "opened": opened,
    "inst": inst,
    "anyline": anyline,
    "bad": bad,
    "base_modules": ip["base_modules"],
    "classes": ip["classes"],
    "preload": preload,
    "files": files,
}
blob = zlib.compress(pickle.dumps(index, protocol=5), 6)
open(OUT, "wb").write(blob)
print("tests", n, "failing alone at the pinned commit:", [ids[i] for i in bad])
print("files", len(files), "classes", len(inst), "opened", len(opened), "bytes", len(blob))
