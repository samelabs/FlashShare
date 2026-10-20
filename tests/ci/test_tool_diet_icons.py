"""Tool diet icon derivation contract (no DB, pure functions).

表达准则: 工具图标 = "这工具吃什么"。派生与格式族词表同源
(governance/formats.py), 字形单源 core/icons.py。本契约锁定:
  1. 八族字形齐备且属 palette;
  2. 派生三档 (text / all→cloud / 族字形) — 无 fallback, 域外输入无定义行为;
  3. FAMILY_GLYPH 与 FAMILY_ORDER 一一对应 (映射单点)。
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


# 2) 派生三档: text / all→cloud / 族字形 (显式 accept 必落单族 — 发布校验保证)
check("text-mode (files_max=0) → text", derived_tool_icon(0, None) == "text")
check("files_max=0 with stray accept still text", derived_tool_icon(0, ["jpg"]) == "text")
check("accept undeclared (=all) → cloud", derived_tool_icon(5, None) == "cloud")
for fam in FAMILY_ORDER:
    key = _FAMILY_GLYPH[fam]
    check(f"single family {fam} → {key}",
          derived_tool_icon(2, [ext_of(fam)]) == key)

print(f"\n{'FAIL' if FAILED else 'PASS'}: tool diet icon contract"
      + (f" — {len(FAILED)} failed: {FAILED}" if FAILED else ""))
sys.exit(1 if FAILED else 0)
