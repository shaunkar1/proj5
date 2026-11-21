"""Parse git-style unified diffs and apply them to text, without git or patch(1)."""

import re

HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    pass


class FilePatch:
    __slots__ = ("old", "new", "hunks", "binary", "created", "deleted", "renamed", "mode_only")

    def __init__(self):
        self.old = None  # path in the pinned tree, None when the file is created
        self.new = None  # path after the change, None when the file is deleted
        self.hunks = []  # (old_start, old_count, new_start, new_count, [(tag, line_bytes)])
        self.binary = False
        self.created = False
        self.deleted = False
        self.renamed = False
        self.mode_only = False


def _path(raw):
    raw = raw.rstrip(b"\r\n")
    if b"\t" in raw:
        raw = raw.split(b"\t", 1)[0]
    raw = raw.strip()
    if raw == b"/dev/null":
        return None
    if raw.startswith(b'"') and raw.endswith(b'"'):
        raw = raw[1:-1].decode("unicode_escape").encode("latin-1")
    text = raw.decode("utf-8", "surrogateescape")
    if text.startswith(("a/", "b/")):
        text = text[2:]
    return text


def parse(data):
    """Return the list of FilePatch objects in a diff (bytes)."""
    lines = data.splitlines(keepends=True)
    patches = []
    current = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith(b"diff --git "):
            current = FilePatch()
            patches.append(current)
            m = re.match(rb"diff --git a/(.*?) b/(.*)$", line.rstrip(b"\r\n"))
            if m:
                current.old = m.group(1).decode("utf-8", "surrogateescape")
                current.new = m.group(2).decode("utf-8", "surrogateescape")
            i += 1
            continue
        if current is None:
            if line.startswith(b"--- ") and i + 1 < n and lines[i + 1].startswith(b"+++ "):
                current = FilePatch()
                patches.append(current)
            else:
                i += 1
                continue
        if line.startswith(b"new file mode"):
            current.created = True
        elif line.startswith(b"deleted file mode"):
            current.deleted = True
        elif line.startswith(b"rename from "):
            current.renamed = True
            current.old = _path(line[len(b"rename from "):])
        elif line.startswith(b"rename to "):
            current.renamed = True
            current.new = _path(line[len(b"rename to "):])
        elif line.startswith(b"copy from ") or line.startswith(b"copy to "):
            current.renamed = True
            if line.startswith(b"copy to "):
                current.new = _path(line[len(b"copy to "):])
        elif line.startswith(b"old mode") or line.startswith(b"new mode"):
            current.mode_only = True
        elif line.startswith(b"Binary files ") or line.startswith(b"GIT binary patch"):
            current.binary = True
        elif line.startswith(b"--- ") and i + 1 < n and lines[i + 1].startswith(b"+++ "):
            old = _path(line[4:])
            new = _path(lines[i + 1][4:])
            if old is None:
                current.created = True
            else:
                current.old = old
            if new is None:
                current.deleted = True
            else:
                current.new = new
            i += 2
            continue
        else:
            m = HUNK.match(line)
            if m:
                old_start = int(m.group(1))
                old_count = int(m.group(2)) if m.group(2) is not None else 1
                new_start = int(m.group(3))
                new_count = int(m.group(4)) if m.group(4) is not None else 1
                body = []
                seen_old = seen_new = 0
                i += 1
                while i < n and (seen_old < old_count or seen_new < new_count):
                    tag = lines[i][:1]
                    if tag == b" " or lines[i] in (b"\n", b"\r\n"):
                        body.append((" ", lines[i][1:] if tag == b" " else lines[i]))
                        seen_old += 1
                        seen_new += 1
                    elif tag == b"-":
                        body.append(("-", lines[i][1:]))
                        seen_old += 1
                    elif tag == b"+":
                        body.append(("+", lines[i][1:]))
                        seen_new += 1
                    elif tag == b"\\":
                        if body:
                            t, text = body[-1]
                            body[-1] = (t, text.rstrip(b"\r\n") if text.endswith(b"\n") else text)
                    else:
                        raise PatchError("bad hunk line")
                    i += 1
                if i < n and lines[i].startswith(b"\\"):
                    if body:
                        t, text = body[-1]
                        body[-1] = (t, text[:-1] if text.endswith(b"\n") else text)
                        if body[-1][1].endswith(b"\r"):
                            pass
                    i += 1
                current.hunks.append((old_start, old_count, new_start, new_count, body))
                current.mode_only = False
                continue
        i += 1
    for p in patches:
        if p.created:
            p.old = None
        if p.deleted:
            p.new = None
    return patches


def apply(old_bytes, file_patch):
    """Apply one file's hunks to its old content and return the new content."""
    old_lines = old_bytes.splitlines(keepends=True) if old_bytes else []
    out = []
    pos = 0  # index into old_lines
    for old_start, old_count, _new_start, _new_count, body in file_patch.hunks:
        start = old_start - 1 if old_count else old_start
        if start < pos or start > len(old_lines):
            raise PatchError("hunk out of order")
        out.extend(old_lines[pos:start])
        pos = start
        for tag, text in body:
            if tag == "+":
                out.append(text)
                continue
            if pos >= len(old_lines):
                raise PatchError("hunk past end of file")
            have = old_lines[pos]
            if have != text and have.rstrip(b"\r\n") != text.rstrip(b"\r\n"):
                raise PatchError("context mismatch")
            if tag == " ":
                out.append(text if have.rstrip(b"\r\n") == text else have)
            pos += 1
    out.extend(old_lines[pos:])
    return b"".join(out)


def changed_old_lines(file_patch):
    """Old-file line numbers touched by the patch: removed lines, plus the two
    neighbours of every pure insertion point."""
    touched = set()
    for old_start, old_count, _ns, _nc, body in file_patch.hunks:
        line = old_start if old_count else old_start + 1
        pending_insert = False
        for tag, _text in body:
            if tag == " ":
                if pending_insert:
                    touched.add(line)
                    touched.add(line - 1)
                    pending_insert = False
                line += 1
            elif tag == "-":
                touched.add(line)
                pending_insert = False
                line += 1
            else:
                pending_insert = True
        if pending_insert:
            touched.add(line - 1)
            touched.add(line)
    return {l for l in touched if l >= 1}
