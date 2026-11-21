"""Decide which tests a patch could possibly break.

The decision uses maps built once from the pinned sqlglot commit: for every
test, run alone, the lines it executes, the functions it enters, the files it
opens and the sqlglot classes it instantiates.  A patch is located in the old
source (astdiff) and matched against those maps.  Nothing here knows about any
particular patch: the answer is always "tests whose own execution reaches the
changed code", and whether they really fail is settled by running them.
"""

import os
import pickle
import zlib

import astdiff
import patchlib

# Class hierarchies whose instances carry class-level settings.  A change in the
# body of such a class is seen by tests that create an instance of the class (or
# of a subclass), or that call code defined in the class.
INSTANCE_ROOTS = (
    "sqlglot.expressions.core:Expr",
    "sqlglot.dialects.dialect:Dialect",
    "sqlglot.parser:Parser",
    "sqlglot.generator:Generator",
    "sqlglot.tokens:Tokenizer",
)

CONFIG_FILES = ("pytest.ini", "tox.ini", "conftest.py", "pyproject.toml", "setup.cfg", "sitecustomize.py")


class Maps:
    def __init__(self, path):
        with open(path, "rb") as handle:
            data = pickle.loads(zlib.decompress(handle.read()))
        self.ids = data["ids"]
        self.cpu = data["cpu"]
        self.opened = data["opened"]
        self.inst = data["inst"]
        self.anyline = data["anyline"]
        self.base_modules = set(data["base_modules"])
        self.classes = data["classes"]
        self.preload = data["preload"]
        self.refs = data.get("refs", {})
        self.times = data.get("times") or [None] * len(data["ids"])
        self.class_setup = data.get("class_setup", {})
        self._files = data["files"]
        self._cache = {}
        self.all = (1 << len(self.ids)) - 1
        self.by_file = {}
        for i, test_id in enumerate(self.ids):
            f = test_id.split("::", 1)[0]
            self.by_file[f] = self.by_file.get(f, 0) | (1 << i)
        self.by_class = {}
        for i, test_id in enumerate(self.ids):
            c = test_id.rsplit("::", 1)[0]
            self.by_class[c] = self.by_class.get(c, 0) | (1 << i)
        self.lazy_load = 0
        for i, test_id in enumerate(self.ids):
            if test_id.endswith("::test_lazy_load"):
                self.lazy_load |= 1 << i

    def file(self, path):
        if path not in self._cache:
            blob = self._files.get(path)
            if blob is None:
                self._cache[path] = (([], {}), ([], {}))
            else:
                self._cache[path] = pickle.loads(zlib.decompress(blob))
        return self._cache[path]

    def covering(self, path, lo, hi):
        (sets, lines), _ = self.file(path)
        mask = 0
        seen = set()
        for line in range(lo, hi + 1):
            k = lines.get(line)
            if k is not None and k not in seen:
                seen.add(k)
                mask |= sets[k]
        return mask

    def entering(self, path, first):
        """Tests that enter the function whose code starts at `first`, or None if unknown."""
        _, (sets, firsts) = self.file(path)
        k = firsts.get(first)
        return None if k is None else sets[k]

    def entering_any(self, path, first_lines):
        _, (sets, firsts) = self.file(path)
        mask = 0
        for first in first_lines:
            k = firsts.get(first)
            if k is not None:
                mask |= sets[k]
        return mask

    def readers(self, names):
        """Tests entering a function that reads one of these class-level names
        (following names derived from them), or None when that cannot be told."""
        todo = list(names)
        seen = set()
        mask = 0
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            entry = self.refs.get(name)
            if entry is None:
                return None
            functions, derived = entry
            for path, first in functions:
                entered = self.entering(path, first)
                if entered:
                    mask |= entered
            todo.extend(derived)
        return mask

    def dir_openers(self, directory):
        mask = 0
        prefix = directory.rstrip("/") + "/"
        for path, m in self.opened.items():
            if path.startswith(prefix) and "/" not in path[len(prefix):]:
                mask |= m
        return mask


class Plan:
    def __init__(self):
        self.cands = 0          # bit mask over map test indices
        self.blind = 0          # candidates that cannot be checked by running
        self.blind_all = False  # give up: select every test
        self.files = {}         # path -> new bytes, or None when deleted
        self.notes = []
        self.touched = set()
        self.runtime_only = True  # every change is inside function bodies
        self.imports = set()      # modules whose import-time code changed
        self.loaders = {}         # module -> tests that load it


def module_name(path):
    name = path[:-3].replace("/", ".")
    if name.endswith(".__init__"):
        name = name[: -len(".__init__")]
    return name


def _is_instance_class(maps, key):
    bases = maps.classes.get(key)
    if bases is None:
        return False
    return key in INSTANCE_ROOTS or any(b in INSTANCE_ROOTS for b in bases)


def _python_file(maps, plan, path, old, new):
    is_test = path.startswith("tests/")
    loaders = maps.anyline.get(path, 0) | maps.opened.get(path, 0)
    if is_test:
        loaders |= maps.by_file.get(path, 0)
    if not is_test:
        plan.loaders[module_name(path)] = loaders | (maps.lazy_load if module_name(path) in maps.base_modules else 0)
    try:
        sites = astdiff.diff(old, new)
    except SyntaxError:
        plan.notes.append("%s: does not parse" % path)
        plan.runtime_only = False
        plan.cands |= loaders | (maps.lazy_load if module_name(path) in maps.base_modules else 0)
        return
    except Exception:
        plan.notes.append("%s: could not compare" % path)
        plan.runtime_only = False
        plan.cands |= loaders | maps.lazy_load
        return
    if not sites:
        plan.notes.append("%s: same program" % path)
        plan.cands |= maps.opened.get(path, 0)
        return
    plan.cands |= maps.opened.get(path, 0)
    try:
        function_spans = astdiff.functions(old)
    except Exception:
        function_spans = []

    def methods_within(lo, hi):
        return maps.entering_any(path, [first for first, _last, _name in function_spans if lo <= first <= hi])

    for site in sites:
        kind = site[0]
        if kind in ("lines", "func"):
            _, lo, hi, first = site
            mask = maps.covering(path, lo, hi)
            entered = maps.entering(path, first)
            if entered is not None:
                mask &= entered
            plan.notes.append("%s:%d-%d in function at %d: %d tests" % (path, lo, hi, first, bin(mask).count("1")))
            plan.cands |= mask
        elif kind == "class":
            _, name, lo, hi, targets = site
            plan.runtime_only = False
            if not is_test:
                plan.imports.add(module_name(path))
            key = module_name(path) + ":" + name
            if is_test:
                mask = maps.inst.get(key, 0) | maps.by_class.get(path + "::" + name.split(".")[0], 0)
                mask |= methods_within(lo, hi)
            elif _is_instance_class(maps, key):
                mask = maps.inst.get(key, 0) | methods_within(lo, hi)
                reading = maps.readers(targets) if targets else None
                if reading is not None:
                    mask &= reading
                if module_name(path) in maps.base_modules:
                    mask |= maps.lazy_load
            else:
                mask = loaders | (maps.lazy_load if module_name(path) in maps.base_modules else 0)
            plan.notes.append("%s: class %s body: %d tests" % (path, name, bin(mask).count("1")))
            plan.cands |= mask
        else:
            plan.runtime_only = False
            if not is_test:
                plan.imports.add(module_name(path))
            mask = loaders | (maps.lazy_load if module_name(path) in maps.base_modules else 0)
            plan.notes.append("%s: module level: %d tests" % (path, bin(mask).count("1")))
            plan.cands |= mask


def analyze(maps, patch_bytes, repo):
    plan = Plan()
    try:
        file_patches = patchlib.parse(patch_bytes)
    except Exception:
        plan.blind_all = True
        plan.notes.append("patch does not parse")
        return plan
    if not file_patches and patch_bytes.strip():
        plan.blind_all = True
        plan.notes.append("no file patches found")
        return plan
    for fp in file_patches:
        path = fp.new or fp.old
        plan.touched.add(path)
        if fp.old and fp.old != path:
            plan.touched.add(fp.old)
        base = os.path.basename(path)
        is_py = path.endswith(".py")
        in_code = path.startswith(("sqlglot/", "tests/"))
        if fp.renamed or (fp.old and fp.new and fp.old != fp.new):
            plan.blind_all = True
            plan.notes.append("%s: renamed" % path)
            continue
        if fp.mode_only and not fp.hunks and not fp.binary:
            continue
        if base in CONFIG_FILES or (is_py and "/" not in path):
            # test configuration, or a new top-level module that could shadow another
            if base in ("pyproject.toml", "setup.cfg") and not any(
                b"pytest" in text or b"unittest" in text
                for hunk in fp.hunks
                for tag, text in hunk[4]
                if tag != " "
            ) and not fp.binary:
                pass
            elif base == "setup.py" and not fp.created:
                pass
            else:
                plan.blind_all = True
                plan.notes.append("%s: configuration" % path)
                continue
        if fp.binary:
            mask = maps.opened.get(path, 0)
            if is_py and in_code:
                plan.blind_all = True
            if path.startswith("tests/") and not mask:
                mask = maps.dir_openers(os.path.dirname(path))
            plan.cands |= mask
            plan.blind |= mask
            plan.notes.append("%s: binary change" % path)
            continue
        old = None
        if not fp.created:
            try:
                with open(os.path.join(repo, path), "rb") as handle:
                    old = handle.read()
            except OSError:
                plan.blind_all = True
                plan.notes.append("%s: not in the pinned tree" % path)
                continue
        if fp.deleted:
            plan.files[path] = None
            plan.runtime_only = False
            mask = maps.anyline.get(path, 0) | maps.opened.get(path, 0) | maps.by_file.get(path, 0)
            if is_py and module_name(path) in maps.base_modules:
                mask |= maps.lazy_load
            if path.startswith("sqlglotc/"):
                mask |= maps.by_file.get("tests/test_sqlglotc.py", 0)
            plan.cands |= mask
            plan.notes.append("%s: deleted" % path)
            continue
        try:
            new = patchlib.apply(old or b"", fp)
        except Exception:
            plan.blind_all = True
            plan.notes.append("%s: patch does not apply" % path)
            continue
        plan.files[path] = new
        if fp.created:
            plan.runtime_only = False
            if is_py and path.startswith("sqlglot/"):
                plan.blind_all = True
                plan.notes.append("%s: new module" % path)
            elif is_py and path.startswith("tests/"):
                if os.path.exists(os.path.join(repo, path[:-3])):
                    plan.blind_all = True  # would shadow a package
                plan.notes.append("%s: new test-side module" % path)
            else:
                plan.cands |= maps.dir_openers(os.path.dirname(path))
                if path.startswith("sqlglotc/"):
                    plan.cands |= maps.by_file.get("tests/test_sqlglotc.py", 0)
                plan.notes.append("%s: new file" % path)
            continue
        if new == old:
            continue
        if is_py and in_code:
            _python_file(maps, plan, path, old, new)
        else:
            mask = maps.opened.get(path, 0)
            if path.startswith("sqlglotc/"):
                mask |= maps.by_file.get("tests/test_sqlglotc.py", 0)
            plan.cands |= mask
            plan.notes.append("%s: data file read by %d tests" % (path, bin(mask).count("1")))
    return plan
