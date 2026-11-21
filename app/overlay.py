"""Build a patched copy of the pinned tree out of symlinks.

Only the directories on the way to a changed file are real; everything else
points back into the read-only checkout, so a tree costs a few dozen symlinks.
"""

import os
import shutil


def build(base, dest, files, copies=()):
    """files: path -> new bytes, or None to leave the file out.
    copies: paths to copy for real instead of linking."""
    real_dirs = {""}
    for path in list(files) + list(copies):
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            real_dirs.add("/".join(parts[:i]))
    os.makedirs(dest)
    for directory in sorted(real_dirs, key=lambda d: d.count("/") if d else -1):
        src = os.path.join(base, directory) if directory else base
        dst = os.path.join(dest, directory) if directory else dest
        if directory:
            os.makedirs(dst, exist_ok=True)
        try:
            names = os.listdir(src)
        except OSError:
            names = []
        for name in names:
            rel = directory + "/" + name if directory else name
            if rel in real_dirs or rel in files or rel in copies:
                continue
            os.symlink(os.path.join(src, name), os.path.join(dst, name))
    for path in copies:
        if path not in files:
            shutil.copyfile(os.path.join(base, path), os.path.join(dest, path))
    for path, data in files.items():
        if data is None:
            continue
        with open(os.path.join(dest, path), "wb") as handle:
            handle.write(data)
    return dest


def remove(dest):
    shutil.rmtree(dest, ignore_errors=True)
