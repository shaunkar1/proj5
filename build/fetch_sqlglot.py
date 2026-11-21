"""Fetch sqlglot at the pinned commit from GitHub and unpack it. Usage: fetch_sqlglot.py <dest>"""
import io
import os
import sys
import tarfile
import time
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URLS = [
    "https://github.com/tobymao/sqlglot/archive/%s.tar.gz" % COMMIT,
    "https://codeload.github.com/tobymao/sqlglot/tar.gz/%s" % COMMIT,
]


def download():
    last = None
    for attempt in range(6):
        url = URLS[attempt % len(URLS)]
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                return response.read()
        except Exception as error:  # network hiccup: try again
            last = error
            time.sleep(3 + 3 * attempt)
    raise SystemExit("could not download sqlglot: %r" % (last,))


def main(dest):
    data = download()
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = []
        for member in archive.getmembers():
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            member.name = parts[1]
            members.append(member)
        archive.extractall(dest, members=members, filter="data")
    for needed in ("sqlglot/__init__.py", "tests/test_parser.py", "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"):
        if not os.path.exists(os.path.join(dest, needed)):
            raise SystemExit("unexpected archive layout: %s missing" % needed)
    print("sqlglot %s unpacked into %s" % (COMMIT[:7], dest))


if __name__ == "__main__":
    main(sys.argv[1])
