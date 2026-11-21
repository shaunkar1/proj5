"""Decide which tests a patch could possibly break.

The decision uses maps built once from the pinned sqlglot commit: for every
test, run alone, the lines it executes, the functions it enters, the files it
opens and the sqlglot classes it instantiates.  A patch is located in the old
source (astdiff) and matched against those maps.  Nothing here knows about any
particular patch: the answer is always "tests whose own execution reaches the
changed code", and whether they really fail is settled by running them.

The same matching is done one level down for the long TPC tests: the map also
holds, for each of their queries, what that query alone reaches, so that only
the queries a patch can affect need to be run.
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

# Experimental: also narrow settings of classes that are instantiated while
# modules are imported, and module-level names read at import time.
FOLLOW_ALL = os.environ.get("BR_FOLLOW_ALL") == "1"

CONFIG_FILES = ("pytest.ini", "tox.ini", "conftest.py", "pyproject.toml", "setup.cfg", "sitecustomize.py")


class View:
    """What each member of a population (tests, or queries of one test) reaches."""

    def __init__(self, size, files, inst, refs, late_files=None, inst_early=None):
        self.size = size
        self.all = (1 << size) - 1
        self._files = files      # path -> packed ((sets, lines), (sets, firsts)) or its zlib pickle
        self._cache = {}
        # lines run after the member's modules were imported, and classes
        # instantiated before that; None when the records make no such split
        self._late = late_files
        self._late_cache = {}
        self.inst_early = inst_early or {}
        self.inst = inst         # class -> mask
        self.refs = refs         # name -> (reader statements, derived names, read at import) or None

    def file(self, path):
        if path not in self._cache:
            blob = self._files.get(path)
            if blob is None:
                self._cache[path] = (([], {}), ([], {}))
            elif isinstance(blob, bytes):
                self._cache[path] = pickle.loads(zlib.decompress(blob))
            else:
                self._cache[path] = blob
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

    def covering_late(self, path, lo, hi):
        """Like covering, but counting only what ran after import time."""
        if self._late is None:
            return self.covering(path, lo, hi)
        if path not in self._late_cache:
            blob = self._late.get(path)
            self._late_cache[path] = pickle.loads(zlib.decompress(blob)) if blob is not None else ([], {})
        sets, lines = self._late_cache[path]
        mask = 0
        seen = set()
        for line in range(lo, hi + 1):
            k = lines.get(line)
            if k is not None and k not in seen:
                seen.add(k)
                mask |= sets[k]
        return mask

    def loading(self, path):
        (sets, _lines), _ = self.file(path)
        mask = 0
        for m in sets:
            mask |= m
        return mask

    def entering(self, path, first):
        """Members that enter the function whose code starts at `first`, or None if unknown."""
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

    def readers(self, names, allow_import_time, early_holders=None):
        """Members executing a statement that reads one of these names, following
        names derived from them; None when that cannot be told.  With
        allow_import_time False, a name read by a function that also runs while
        modules are imported counts as untraceable.

        early_holders: when given, the name belongs to a class, and a read made
        while modules are imported only counts for members that already held an
        instance of that class then (the bit mask given)."""
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
            statements, derived, at_import = entry
            if at_import and not allow_import_time:
                return None
            for path, first, lo, hi in statements:
                if early_holders is None:
                    reached = self.covering(path, lo, hi)
                else:
                    reached = self.covering_late(path, lo, hi) | (self.covering(path, lo, hi) & early_holders)
                entered = self.entering(path, first)
                if entered is not None:
                    reached &= entered
                mask |= reached
            todo.extend(derived)
            if len(seen) > 40:
                return None
        return mask


class Maps:
    def __init__(self, path):
        with open(path, "rb") as handle:
            data = pickle.loads(zlib.decompress(handle.read()))
        self.ids = data["ids"]
        self.times = data.get("times") or [None] * len(self.ids)
        self.class_setup = data.get("class_setup", {})
        self.opened = data["opened"]
        self.anyline = data["anyline"]
        self.base_modules = set(data["base_modules"])
        self.classes = data["classes"]
        self.preload = data["preload"]
        self.at_import = set(data.get("instantiated_at_import", ()))
        self.thinnable = set(data.get("thinnable", ()))
        self.holders = data.get("holders", {})
        refs = data.get("refs", {})
        self.view = View(len(self.ids), data["files"], data["inst"], refs, data.get("late_files"), data.get("inst_early"))
        self.all = self.view.all
        # per-query views of the long TPC tests: method -> (View, cpu seconds per query, fixed cpu)
        # comparison place -> kinds of operands seen there (tools/swapmap.py)
        self.swaps = {}
        try:
            import json

            with open(os.path.join(os.path.dirname(path), "swaps.json")) as handle:
                self.swaps = json.load(handle)
        except Exception:
            self.swaps = {}
        self.queries = {}
        for method, sub in data.get("queries", {}).items():
            self.queries[method] = (View(sub["n"], sub["files"], sub["inst"], refs), sub["cpu"], sub["base_cpu"])
        self.by_file = {}
        self.by_class = {}
        self.lazy_load = 0
        for i, test_id in enumerate(self.ids):
            f = test_id.split("::", 1)[0]
            self.by_file[f] = self.by_file.get(f, 0) | (1 << i)
            c = test_id.rsplit("::", 1)[0]
            self.by_class[c] = self.by_class.get(c, 0) | (1 << i)
            if test_id.endswith("::test_lazy_load"):
                self.lazy_load |= 1 << i

    def package_users(self, directory):
        """Tests that load any module of a package, or live in it."""
        prefix = directory.rstrip("/") + "/"
        mask = 0
        for path, m in self.anyline.items():
            if path.startswith(prefix):
                mask |= m
        for path, m in self.by_file.items():
            if path.startswith(prefix):
                mask |= m
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
        self.imports = set()    # modules whose import-time code changed
        self.loaders = {}       # module -> tests that load it
        self.python = []        # (path, sites, function spans) of changed python files
        self.plain = True       # every candidate comes from located python changes
        self.fixture = {}       # changed fixture file -> tests reading it
        self.other_mask = 0     # candidates from anything but a changed fixture file


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


def site_mask(maps, view, path, site, function_spans, loaders, notes=None):
    """Members of `view` that can see the change at one site of a sqlglot or test
    source file.  `loaders` are the members that load the file at all."""
    is_test = path.startswith("tests/")
    kind = site[0]
    if kind == "enter":
        mask = view.entering(path, site[1]) or 0
        what = "signature of function at %d" % site[1]
    elif kind in ("lines", "func"):
        _, lo, hi, first = site
        mask = view.covering(path, lo, hi)
        entered = view.entering(path, first)
        if entered is not None:
            mask &= entered
        what = "%d-%d in function at %d" % (lo, hi, first)
    elif kind == "class":
        _, name, lo, hi, targets = site
        key = module_name(path) + ":" + name
        methods = view.entering_any(path, [first for first, _last, _name in function_spans if lo <= first <= hi])
        if is_test:
            if view is maps.view:
                mask = view.inst.get(key, 0) | methods | maps.by_class.get(path + "::" + name.split(".")[0], 0)
            else:
                mask = loaders
        elif _is_instance_class(maps, key):
            mask = view.inst.get(key, 0) | methods
            early = view.inst_early.get(key, 0)
            # a dialect reads settings of its tokenizer, parser and generator
            # classes without making instances of them
            for holder in maps.holders.get(key, ()):
                mask |= view.inst.get(holder, 0)
                early |= view.inst_early.get(holder, 0)
            # Instances made while modules are imported could carry the old
            # value into import-time state; then nothing can be narrowed.
            reading = view.readers(targets, True, early) if targets and (FOLLOW_ALL or key not in maps.at_import) else None
            if reading is not None:
                mask &= reading
        else:
            mask = loaders
        what = "class %s body" % name
    else:
        _, lo, hi, targets = site
        mask = loaders
        reading = view.readers(targets, FOLLOW_ALL) if targets and not is_test else None
        if reading is not None:
            mask &= reading
        what = "module level"
    if notes is not None:
        notes.append("%s: %s: %d tests" % (path, what, bin(mask).count("1")))
    return mask


def _python_file(maps, plan, path, old, new):
    is_test = path.startswith("tests/")
    in_base = (not is_test) and module_name(path) in maps.base_modules
    loaders = maps.anyline.get(path, 0) | maps.opened.get(path, 0)
    if is_test:
        loaders |= maps.by_file.get(path, 0)
    if os.path.basename(path) == "__init__.py":
        loaders |= maps.package_users(os.path.dirname(path))
    if not is_test:
        plan.loaders[module_name(path)] = loaders | (maps.lazy_load if in_base else 0)
    try:
        sites = astdiff.diff(old, new)
    except SyntaxError:
        plan.notes.append("%s: does not parse" % path)
        plan.plain = False
        plan.cands |= loaders | (maps.lazy_load if in_base else 0)
        return
    except Exception:
        plan.notes.append("%s: could not compare" % path)
        plan.plain = False
        plan.cands |= loaders | maps.lazy_load
        return
    if maps.opened.get(path, 0):
        plan.cands |= maps.opened[path]
        plan.plain = False
    if not sites:
        plan.notes.append("%s: same program" % path)
        return
    try:
        function_spans = astdiff.functions(old)
    except Exception:
        function_spans = []
    plan.python.append((path, sites, function_spans))
    for site in sites:
        plan.cands |= site_mask(maps, maps.view, path, site, function_spans, loaders, plan.notes)
        if site[0] in ("enter", "class", "module") and not is_test:
            # import-time code changed: check that the module still imports
            plan.imports.add(module_name(path))
            if in_base and site[0] != "enter":
                plan.cands |= maps.lazy_load


def query_mask(maps, plan, method):
    """Queries of a long TPC test that can see the patch: a bit mask, or None
    when every query has to be assumed affected."""
    entry = maps.queries.get(method)
    if entry is None or not plan.plain or not plan.python:
        return None
    view = entry[0]
    mask = 0
    settings_only = True
    for path, sites, function_spans in plan.python:
        if path.startswith("tests/"):
            return None
        loaders = view.loading(path)
        for site in sites:
            mask |= site_mask(maps, view, path, site, function_spans, loaders)
            if site[0] != "class" or not site[4]:
                settings_only = False
    if mask or settings_only:
        # A changed class setting matters to a position only if that position
        # both holds the class and reads the setting; the test as a whole can do
        # each in different positions, and then none is affected.
        return mask
    # Changed code that the test reaches but no single position does is
    # unexpected: assume every position is affected.
    return None


def analyze(maps, patch_bytes, repo):
    plan = _analyze(maps, patch_bytes, repo)
    return plan


def _analyze(maps, patch_bytes, repo):
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
    harmless = _harmless_rewrites(maps, file_patches, repo)
    for fp in file_patches:
        before = plan.cands
        plan.cands = 0
        if (fp.new or fp.old) in harmless:
            path = fp.new or fp.old
            plan.touched.add(path)
            plan.files[path] = harmless[path][0]
            plan.notes.append("%s: identity comparison rewritten as equality at line %s: same result for every operand seen there" % (path, ", ".join(map(str, harmless[path][1]))))
        else:
            _one_file(maps, plan, fp, repo)
        path = fp.new or fp.old
        simple_fixture = (
            path.startswith("tests/fixtures/")
            and not (fp.binary or fp.created or fp.deleted or fp.renamed)
            and path in plan.files
        )
        if simple_fixture:
            plan.fixture[path] = plan.fixture.get(path, 0) | plan.cands
        else:
            plan.other_mask |= plan.cands
        plan.cands |= before
    return plan


def _same_result(pair):
    """Do `x is y` and `x == y` agree for operands of these kinds?  pair is
    (type of x, class defining its __eq__, x is an Enum member, the same for y),
    as recorded for one comparison over the whole suite at the pinned commit."""
    left, left_eq, left_enum, right, right_eq, right_enum = pair
    none = "builtins:NoneType"

    def never_equals_none(eq):
        # built-in equality, and sqlglot's expression equality (same type and same hash)
        return eq.startswith("builtins:") or eq == "sqlglot.expressions.core:Expression"

    if left_eq == "builtins:object" and right_eq == "builtins:object":
        return True  # neither side defines equality: it is identity
    if left == "builtins:bool" and right == "builtins:bool":
        return True  # True and False are single objects
    if right == none:
        return left == none or never_equals_none(left_eq)
    if left == none:
        return never_equals_none(right_eq)
    if right_enum:
        # two members of one Enum are equal only when identical; an object with
        # plain identity equality equals nothing else
        return left == right or left_eq == "builtins:object"
    if left_enum:
        return right_eq == "builtins:object"
    return False


def _harmless_rewrites(maps, file_patches, repo):
    """Files of the patch in which all that happens is that `is` comparisons
    become `==` (or `is not` becomes `!=`) at places where, for every pair of
    operands any test puts there at the pinned commit, both give the same
    result.  A test that reads none of the patch's other files then runs exactly
    as before, step by step, so it cannot change its outcome.

    Returns {path: (new source, places)}; empty unless every source file in
    the patch qualifies and the rest are edits to existing fixture files."""
    swaps = maps.swaps
    if not swaps or not file_patches:
        return {}
    found = {}
    try:
        for fp in file_patches:
            path = fp.new or fp.old
            if fp.old != fp.new or fp.renamed or fp.binary or fp.created or fp.deleted or fp.mode_only:
                return {}
            if path.startswith("tests/fixtures/"):
                continue
            if not (path.startswith("sqlglot/") and path.endswith(".py")) or maps.opened.get(path, 0):
                return {}
            with open(os.path.join(repo, path), "rb") as handle:
                old = handle.read()
            new = patchlib.apply(old, fp)
            places = []
            for site in astdiff.diff(old, new, keep_swaps=True):
                if site[0] != "swap":
                    return {}
                seen = swaps.get(path, {}).get("%d:%d" % (site[1], site[2]))
                if not seen or not all(_same_result(pair) for pair in seen):
                    return {}
                places.append(site[1])
            if places:
                found[path] = (new, places)
            elif new != old and astdiff.diff(old, new):
                return {}
    except Exception:
        return {}
    return found


def _one_file(maps, plan, fp, repo):
    if True:
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
            return
        if fp.mode_only and not fp.hunks and not fp.binary:
            return
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
                return
        if fp.binary:
            plan.plain = False
            mask = maps.opened.get(path, 0)
            if is_py and in_code:
                plan.blind_all = True
            if path.startswith("tests/") and not mask:
                mask = maps.dir_openers(os.path.dirname(path))
            plan.cands |= mask
            plan.blind |= mask
            plan.notes.append("%s: binary change" % path)
            return
        old = None
        if not fp.created:
            try:
                with open(os.path.join(repo, path), "rb") as handle:
                    old = handle.read()
            except OSError:
                plan.blind_all = True
                plan.notes.append("%s: not in the pinned tree" % path)
                return
        if fp.deleted:
            plan.files[path] = None
            plan.plain = False
            mask = maps.anyline.get(path, 0) | maps.opened.get(path, 0) | maps.by_file.get(path, 0)
            if base == "__init__.py":
                mask |= maps.package_users(os.path.dirname(path))
            if is_py and module_name(path) in maps.base_modules:
                mask |= maps.lazy_load
            if path.startswith("sqlglotc/"):
                mask |= maps.by_file.get("tests/test_sqlglotc.py", 0)
            plan.cands |= mask
            plan.notes.append("%s: deleted" % path)
            return
        try:
            new = patchlib.apply(old or b"", fp)
        except Exception:
            plan.blind_all = True
            plan.notes.append("%s: patch does not apply" % path)
            return
        plan.files[path] = new
        if fp.created:
            plan.plain = False
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
            return
        if new == old:
            return
        if is_py and in_code:
            _python_file(maps, plan, path, old, new)
        else:
            mask = maps.opened.get(path, 0)
            if path.startswith("sqlglotc/"):
                mask |= maps.by_file.get("tests/test_sqlglotc.py", 0)
            if mask:
                plan.plain = False
            plan.cands |= mask
            plan.notes.append("%s: data file read by %d tests" % (path, bin(mask).count("1")))
