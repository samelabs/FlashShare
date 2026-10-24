#!/usr/bin/env python3
"""Regenerate the i18n artifacts from core/i18n.py SERVER_I18N.

SERVER_I18N (core/i18n.py) stays the ONLY authority. Generated shape
(on-demand loading): static/i18n.js ships the engine-side fallback pack
(English) for every first screen; static/i18n/<lang>.js carries each
language pack separately (including en, for symmetry and integrity
checks). Pages inject the current language's shard server-side when it
isn't en; the switcher loads other shards on demand. Do not hand-edit
generated files. `--check` verifies the committed artifacts match the
source (used by CI) without writing.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core"))
from i18n import SERVER_I18N  # noqa: E402

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def _pack(lang: str) -> str:
    # Key order follows source dict order (insertion order), same as the
    # historical single-file artifact.
    return json.dumps(dict(SERVER_I18N[lang]), ensure_ascii=False,
                      separators=(",", ":"))


def _artifacts() -> dict:
    out = {"i18n.js": "// AUTO-GENERATED from core/i18n.py SERVER_I18N — do not edit by hand.\n"
                      "// First-screen pack: English (the resolver's fallback) only;\n"
                      "// every other language lives in static/i18n/<lang>.js.\n"
                      "window.__I18N__ = {\"en\":" + _pack("en") + "};\n"}
    for lang in SERVER_I18N:
        out[os.path.join("i18n", f"{lang}.js")] = (
            "// AUTO-GENERATED from core/i18n.py SERVER_I18N — do not edit by hand.\n"
            "window.__I18N__=window.__I18N__||{};"
            f"window.__I18N__[\"{lang}\"]={_pack(lang)};\n")
    return out


def main() -> int:
    files = _artifacts()
    if "--check" in sys.argv:
        bad = []
        for rel, content in files.items():
            path = os.path.join(_ROOT, rel)
            try:
                if open(path, encoding="utf-8").read() != content:
                    bad.append(rel)
            except OSError:
                bad.append(rel + " (missing)")
        if bad:
            print("i18n artifacts out of date:", *bad, sep="\n  ")
            return 1
        print("i18n artifacts in sync:", len(files), "files,",
              len(SERVER_I18N), "languages")
        return 0
    for rel, content in files.items():
        path = os.path.join(_ROOT, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    print("regenerated:", len(files), "files,", len(SERVER_I18N), "languages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
