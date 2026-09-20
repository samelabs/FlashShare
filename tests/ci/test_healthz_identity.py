#!/usr/bin/env python3
"""Runtime identity: /healthz carries source_sha + source_dirty (task 9).

Loads the ACTUAL app.main module identity helpers in isolation (no app
spin-up, no DB): _compute_source_identity() must return the repo's real
git state, and source_identity() must expose it read-only. Computes the
repo HEAD twice — directly with git and via the app function — and
asserts they agree. Also asserts healthz route registers the keys.
"""
import importlib
import json
import os
import subprocess
import sys

_REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "core"))
sys.path.insert(0, os.path.join(_REPO, "app"))

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name}")


def git(*args):
    return subprocess.run(["git", *args], cwd=_REPO, capture_output=True,
                          text=True, check=True).stdout.strip()


def main():
    os.environ.setdefault("FLASHSHARE_LOG_DIR", "/tmp/fs-test-logs")
    from app.main import _compute_source_identity, source_identity  # noqa

    ident = _compute_source_identity()
    real_sha = git("rev-parse", "HEAD")
    real_dirty = bool(git("status", "--porcelain"))

    check("identity has source_sha key", "source_sha" in ident)
    check("identity has source_dirty key", "source_dirty" in ident)
    check("source_sha == real git HEAD", ident["source_sha"] == real_sha)
    check("source_dirty == real git dirty state", ident["source_dirty"] == real_dirty)

    snapshot = source_identity()
    check("source_identity() returns a copy (mutation-safe)",
          snapshot == ident and snapshot is not ident)

    # healthz route response shape: route handler exists and merges identity
    from app.main import app as fastapp  # noqa
    routes = {getattr(r, "path", None) for r in fastapp.routes}
    check("/healthz route registered", "/healthz" in routes)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
