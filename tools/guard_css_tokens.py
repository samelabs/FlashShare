#!/usr/bin/env python3
"""Guard: tokens.css is the single design-variable authority.

Checks, across every CSS file under static/:
  a) no :root variable block outside static/css/tokens.css;
  b) every var(--name) reference resolves to a variable defined in
     tokens.css (fallbacks do not excuse an unknown name);
  c) no raw hex colors outside tokens.css (rgba()/named colors are
     allowed for transparency tints; hex is not).
Exit 0 = clean; exit 1 = violations (listed).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = ROOT / "static" / "css" / "tokens.css"
CSS_FILES = sorted((ROOT / "static").rglob("*.css"))

_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b")
_VAR = re.compile(r"var\((--[\w-]+)")
_ROOT = re.compile(r"(?m)^\s*:root\s*\{")


def strip_comments(src: str) -> str:
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


def main() -> int:
    tokens_src = strip_comments(TOKENS.read_text(encoding="utf-8"))
    _var_def = re.compile(r"(--[\w-]+)\s*:")
    defined = set(_var_def.findall(tokens_src))
    if not defined:
        print("guard: no variables found in tokens.css — parser broken")
        return 1

    bad: list[str] = []
    for f in CSS_FILES:
        src = strip_comments(f.read_text(encoding="utf-8"))
        rel = f.relative_to(ROOT)
        if f != TOKENS:
            if _ROOT.search(src):
                bad.append(f"{rel}: :root block outside tokens.css")
            for m in _HEX.finditer(src):
                bad.append(f"{rel}: raw hex color {m.group(0)}")
        for m in _VAR.finditer(src):
            if m.group(1) not in defined:
                bad.append(f"{rel}: unresolved {m.group(0)})")

    if bad:
        print(f"css token guard: {len(bad)} violation(s)")
        for b in bad:
            print("  " + b)
        return 1
    n_vars = len(defined)
    print(f"css token guard: clean ({len(CSS_FILES)} files, {n_vars} variables, single :root in tokens.css)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
