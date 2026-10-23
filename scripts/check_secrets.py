#!/usr/bin/env python3
"""Reject credential paths/values without printing any credential content."""

import argparse
import re
import subprocess
from pathlib import Path, PurePosixPath

TOKEN = re.compile(rb"sk-[A-Za-z0-9_-]{20,}")
PRIVATE_KEY = re.compile(rb"(?m)^-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----\r?$")


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def protected(path):
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    return (
        "infer_apikey_donotupload" in parts
        or (name == ".env" or name.startswith(".env."))
        and name not in {".env.example", ".env.template"}
        or path.endswith(".streamlit/secrets.toml")
        or name.endswith((".pem", ".key"))
    )


def local_secrets(root):
    values = set()
    directory = root / "infer_apikey_donotupload"
    if directory.exists():
        for path in directory.glob("*.txt"):
            raw = path.read_bytes().strip()
            values.update(TOKEN.findall(raw))
            if len(raw) >= 20 and not any(c in raw for c in (b"\n", b"\r", b" ")):
                values.add(raw)
    return values


def entries(tree):
    for item in git("ls-tree", "-rz", "--full-tree", tree).split(b"\0"):
        if not item:
            continue
        metadata, path = item.split(b"\t", 1)
        _, kind, oid = metadata.split()
        if kind == b"blob":
            yield oid.decode(), path.decode("utf-8", errors="replace")


def staged_entries():
    for item in git("ls-files", "--stage", "-z").split(b"\0"):
        if not item:
            continue
        metadata, path = item.split(b"\t", 1)
        _, oid, stage = metadata.split()
        if stage != b"0":
            raise RuntimeError("Unmerged index; resolve conflicts before scanning.")
        yield oid.decode(), path.decode("utf-8", errors="replace")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history",
        action="store_true",
        help="Scan all locally reachable commit trees before publishing.",
    )
    args = parser.parse_args()
    root = Path(git("rev-parse", "--show-toplevel").decode().strip())
    secrets = local_secrets(root)
    checked_blobs = {}
    failures = set()
    checked_paths = 0
    if args.history:
        commits = git("rev-list", "--all", "HEAD").decode().splitlines()
        sources = (entries(commit) for commit in commits)
    else:
        sources = (staged_entries(),)
    for source in sources:
        for oid, path in source:
            checked_paths += 1
            if protected(path):
                failures.add((path, "protected credential path"))
            if oid not in checked_blobs:
                content = git("cat-file", "blob", oid)
                checked_blobs[oid] = bool(
                    TOKEN.search(content)
                    or PRIVATE_KEY.search(content)
                    or any(value in content for value in secrets)
                )
            if checked_blobs[oid]:
                failures.add((path, "possible credential content"))
    if failures:
        for path, reason in sorted(failures):
            print(f"BLOCKED: {path!r}: {reason}")
        print("No matching values were printed. Remove secrets before proceeding.")
        return 1
    mode = "reachable history" if args.history else "Git index"
    print(f"PASS: {mode}; {checked_paths} paths, {len(checked_blobs)} unique blobs.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError, RuntimeError):
        # Do not print exceptions which might include payloads or credentials.
        print("BLOCKED: scan could not complete; check repository/access/conflicts.")
        raise SystemExit(2) from None
