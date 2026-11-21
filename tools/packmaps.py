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

# Where class-level and module-level names are read.  For every name assigned in
# a class body or at module level somewhere in sqlglot: the functions that
# mention it, and the names whose values are derived from it (assigned from it in
# a class body, or stored as attributes by a function that reads it, such as a
# metaclass).  A name that is used bare at module level cannot be followed and
# is stored as None.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import astdiff

class_names, module_names = set(), set()
trees = {}
for path in sorted(glob.glob(os.path.join(ROOT, "sqlglot", "**", "*.py"), recursive=True) + glob.glob(os.path.join(ROOT, "tests", "**", "*.py"), recursive=True)):
    rel = os.path.relpath(path, ROOT)
    trees[rel] = ast.parse(open(path).read())
    if rel.startswith("sqlglot/"):
        for st in trees[rel].body:
            module_names.update(astdiff.assigned_names(st) or ())
        for node in ast.walk(trees[rel]):
            if isinstance(node, ast.ClassDef):
                for st in node.body:
                    class_names.update(astdiff.assigned_names(st) or ())
names = class_names | module_names
import_started = {(f, l) for f, l, _name in ip["started"]}
readers = {name: set() for name in names}   # name -> {(file, function first line, lo, hi)}
edges = {name: set() for name in names}
blocked = set()
statements = {}  # (function, statement id) -> [names read, names stored]
stores_in = {}   # function -> names it stores as attributes

def visit(rel, node, func, targets, block):
    for field, value in ast.iter_fields(node):
        children = value if isinstance(value, list) else [value]
        for child in children:
            if not isinstance(child, ast.AST):
                continue
            c_func, c_targets, c_block = func, targets, block
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and field == "body":
                c_func, c_targets = (rel, astdiff._first_line(node)), None
                c_block = child if isinstance(child, astdiff._BLOCKS) else node.body
            elif func is None and isinstance(node, (ast.ClassDef, ast.Module)) and field == "body":
                c_targets = astdiff.assigned_names(child) or ()
            elif func is not None and isinstance(child, astdiff._BLOCKS):
                c_block = child
            name = None
            store = False
            if isinstance(child, ast.Attribute):
                name = child.attr
                store = isinstance(child.ctx, ast.Store)
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                name = child.id
            elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                name = child.value
            if name in readers:
                if c_func is not None:
                    entry = statements.setdefault((c_func, id(c_block)), [set(), set()])
                    if store:
                        stores_in.setdefault(c_func, set()).add(name)
                        entry[1].add(name)
                    else:
                        if isinstance(c_block, astdiff._BLOCKS):
                            lo, hi = astdiff._header_span(c_block)
                        else:
                            lo, hi = c_block.lineno, c_block.end_lineno
                        readers[name].add((rel, c_func[1], lo, hi))
                        entry[0].add(name)
                elif c_targets:
                    edges[name].update(c_targets)
                elif not (isinstance(child, ast.Constant)):
                    blocked.add(name)
            visit(rel, child, c_func, c_targets, c_block)

for rel, tree in trees.items():
    visit(rel, tree, None, None, None)
refs = {}
for name in names:
    if name in blocked or len(readers[name]) > 400:
        refs[name] = None
    else:
        refs[name] = (sorted(readers[name]), sorted(edges[name] - {name}), any((r[0], r[1]) in import_started for r in readers[name]))
print("names", len(names), "followable", sum(v is not None for v in refs.values()), "of which read at import", sum(bool(v and v[2]) for v in refs.values()))

timing = json.load(open(sys.argv[5])) if len(sys.argv) > 5 else {"tests": {}, "classes": {}}
times = timing["tests"]

queries = {}
if len(sys.argv) > 6:
    for method, sub in json.load(open(sys.argv[6])).items():
        sub_files = {}
        for f in set(sub["lines"]) | set(sub["started"]):
            sub_files[f] = (pack({int(l): int(m, 16) for l, m in sub["lines"].get(f, {}).items()}),
                            pack({int(l): int(m, 16) for l, m in sub["started"].get(f, {}).items()}))
        queries[method] = {"n": sub["n"], "cpu": sub["cpu"], "base_cpu": sub["base_cpu"], "files": sub_files,
                           "inst": {c: int(m, 16) for c, m in sub["inst"].items()}}
        print("queries", method, sub["n"], "cpu", round(sum(sub["cpu"]), 1))

thinnable = json.load(open(sys.argv[7])) if len(sys.argv) > 7 else []

index = {
    "thinnable": thinnable,
    "queries": queries,
    "refs": refs,
    "instantiated_at_import": ip["instantiated"],
    "holders": ip["holders"],
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
