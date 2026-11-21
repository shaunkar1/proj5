"""Offline: for every `is` / `is not` comparison in sqlglot's source, the kinds of
operands the test suite puts there at the pinned commit.

1. Copy the checkout, rewrite each such comparison in the copy's sqlglot/ into a
   call (tools/_brprof.py) that notes the type of each operand, the class that
   defines its __eq__ and whether it is an Enum member, then compares as before.
2. Run every listed test on the copy (all must pass).
3. Fold the notes into maps/swaps.json: file -> "line:col" -> list of operand
   kinds, or null when there are too many to list.

Usage: swapmap.py rewrite <copy of checkout>
       swapmap.py pack <notes file> <out.json>
"""
import ast, glob, json, os, sys

class Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        site = "%s:%d:%d" % (self.rel, node.lineno, node.col_offset)
        self.generic_visit(node)
        if len(node.ops) == 1 and isinstance(node.ops[0], (ast.Is, ast.IsNot)):
            call = ast.Call(func=ast.Name(id="__br_is__", ctx=ast.Load()), args=[node.left, node.comparators[0], ast.Constant(site)], keywords=[])
            if isinstance(node.ops[0], ast.IsNot):
                call = ast.UnaryOp(op=ast.Not(), operand=call)
            return ast.copy_location(call, node)
        return node


def rewrite(root):
    import shutil

    shutil.copyfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_brprof.py"), os.path.join(root, "_brprof.py"))
    for path in sorted(glob.glob(os.path.join(root, "sqlglot", "**", "*.py"), recursive=True)):
        rel = os.path.relpath(path, root)
        tree = Rewriter(rel).visit(ast.parse(open(path).read()))
        at, body = 0, tree.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            at = 1
        while at < len(body) and isinstance(body[at], ast.ImportFrom) and body[at].module == "__future__":
            at += 1
        body.insert(at, ast.ImportFrom(module="_brprof", names=[ast.alias(name="is_", asname="__br_is__")], level=0))
        ast.fix_missing_locations(tree)
        open(path, "w").write(ast.unparse(tree) + "\n")


def pack(notes, out):
    seen = {}
    for line in open(notes):
        try:
            site, *pair = json.loads(line)
        except ValueError:
            continue
        path, line_no, col = site.rsplit(":", 2)
        seen.setdefault(path, {}).setdefault("%s:%s" % (line_no, col), set()).add(tuple(pair))
    result = {}
    for path, places in seen.items():
        result[path] = {place: (sorted(map(list, pairs)) if len(pairs) <= 12 and not any("?" in p for p in pairs) else None) for place, pairs in places.items()}
    json.dump(result, open(out, "w"), separators=(",", ":"), sort_keys=True)
    print("files", len(result), "places", sum(len(v) for v in result.values()), "too many kinds", sum(1 for v in result.values() for x in v.values() if x is None))


if __name__ == "__main__":
    if sys.argv[1] == "rewrite":
        rewrite(sys.argv[2])
    else:
        pack(sys.argv[2], sys.argv[3])
