#!/usr/bin/env python3
"""Regenerate static/i18n.js from core/i18n.py SERVER_I18N.
Format matches the existing AUTO-GENERATED artifact exactly."""
import json
import sys

sys.path.insert(0, "/opt/flashshare/core")
from i18n import SERVER_I18N  # noqa: E402

# Key order follows source dict order (insertion order) — same as the
# historical artifact (keys appended per-commit in source order).
out = "window.__I18N__ = " + json.dumps(
    {lang: dict(kv) for lang, kv in SERVER_I18N.items()},
    ensure_ascii=False, separators=(",", ":")) + ";"

with open("/opt/flashshare/static/i18n.js", "w", encoding="utf-8") as f:
    f.write("// AUTO-GENERATED from core/i18n.py SERVER_I18N — do not edit by hand.\n")
    f.write(out + "\n")

print("regenerated, bytes:", len(out))
