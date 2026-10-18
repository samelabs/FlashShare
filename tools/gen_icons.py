#!/usr/bin/env python3
"""Regenerate static/pages/icons.js from core/icons.py _ICONS.

前端 window.__ICONS__ 注册表的唯一生成器: 服务端 palette (core/icons.py)
是单源, icons.js 是它的客户端投影 — 手编 icons.js 即漂移
(2026-10-18 五枚族字形只进服务端、前端全落 info 兜底即事故样本)。
加图标 = 改 core/icons.py, 然后 `venv/bin/python tools/gen_icons.py`。
"""
import json
import os
import sys

import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core"))
from icons import _ICONS  # noqa: E402

out = "window.__ICONS__ = " + json.dumps(
    {k: dict(_ICONS)[k] for k in _ICONS},
    ensure_ascii=False, separators=(",", ":")) + ";"

path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "static", "pages", "icons.js")
with open(path, "w", encoding="utf-8") as f:
    f.write(out + "\n")
print("regenerated, icons:", len(_ICONS))
