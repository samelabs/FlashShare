"""Tool diet icon derivation contract (no DB, pure functions).

表达准则: 工具图标 = "这工具吃什么"。派生与格式族词表同源
(governance/formats.py), 字形单源 core/icons.py。本契约锁定:
  1. 七族字形齐备且属 palette;
  2. 派生四档 (text / wand / 族字形 / convert);
  3. 派生任意输入永不离开 palette;
  4. FAMILY_GLYPH 与 FAMILY_ORDER 一一对应 (映射单点)。
"""
import os
import sys

_sys_core = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core")
sys.path.insert(0, _sys_core)

# config.py 在 import 期强制要求的运行环境 (本契约只测纯函数, 给最小占位)
os.environ.setdefault("FLASHSHARE_BASE_URL", "http://ci-contract.local")
os.environ.setdefault("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:1")
os.environ.setdefault("FLASHSHARE_GOKAPI_KEY", "ci-contract")
os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "ci-contract")

from icons import _ICONS, _FAMILY_GLYPH, derived_tool_icon  # noqa: E402
from governance.formats import FAMILIES, FAMILY_ORDER  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("✓" if cond else "✗") + " " + name + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


# 1) 七族字形齐备 (键即族键, document → doc 例外) 且属 palette
for fam in FAMILY_ORDER:
    key = _FAMILY_GLYPH[fam]
    check(f"family glyph '{fam}' → palette '{key}'", key in _ICONS)
check("FAMILY_GLYPH covers FAMILY_ORDER exactly",
      set(_FAMILY_GLYPH) == set(FAMILY_ORDER))


def ext_of(fam):
    return "." + sorted(FAMILIES[fam].exts)[0]


# 2) 派生四档
check("text-mode (files_max=0) → text", derived_tool_icon(0, None) == "text")
check("files_max=0 with stray accept still text", derived_tool_icon(0, ["jpg"]) == "text")
check("undeclared accept → wand", derived_tool_icon(5, None) == "wand")
check("empty accept list → wand", derived_tool_icon(2, []) == "wand")
check("single family image → image", derived_tool_icon(3, [ext_of("image")]) == "image")
check("single family document → doc", derived_tool_icon(1, [ext_of("document")]) == "doc")
check("single family text → code glyph", derived_tool_icon(1, [ext_of("text")]) == "code")
check("single family audio → audio", derived_tool_icon(1, [ext_of("audio")]) == "audio")
check("single family video → video", derived_tool_icon(1, [ext_of("video")]) == "video")
check("multi family → convert",
      derived_tool_icon(2, [ext_of("image"), ext_of("text")]) == "convert")

# 3) 任意输入 (含脏值) 永不离开 palette
for args in [(1, ["zzz"]), (None, None), ("x", 7), (1, None), (3, [".jpg", 42])]:
    out = derived_tool_icon(*args)
    check(f"derivation({args!r}) stays in palette", out in _ICONS, f"got {out!r}")

print(f"\n{'FAIL' if FAILED else 'PASS'}: tool diet icon contract"
      + (f" — {len(FAILED)} failed: {FAILED}" if FAILED else ""))
sys.exit(1 if FAILED else 0)
