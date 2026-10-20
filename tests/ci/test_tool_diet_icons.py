"""Tool icon contract: glyph = declared-tag table lookup (no DB).

表达准则: 工具图标 = 声明 tag 的查表 (输入类型映射的唯一来源,
字形单源 core/icons.py)。本契约锁定:
  1. 九 tag 字形齐备且属 palette (all → cloud);
  2. 纯文本输入 (files_max=0) → text, 先于 tag;
  3. 无兜底: 文件工具缺 tag = 数据违约, KeyError fail loud。
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

from icons import _ICONS, _TAG_GLYPH, tool_glyph  # noqa: E402
from governance.formats import FAMILY_ORDER  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("✓" if cond else "✗") + " " + name + (f" — {detail}" if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


# 1) 九 tag 字形齐备且属 palette; all → cloud
for tag, glyph in _TAG_GLYPH.items():
    check(f"tag '{tag}' → palette '{glyph}'", glyph in _ICONS)
check("tag 词表 = 八场景 + all",
      set(_TAG_GLYPH) == set(FAMILY_ORDER) | {"all"})
check("all → cloud", _TAG_GLYPH["all"] == "cloud")

# 2) 纯文本输入 → text, 先于 tag (tag 传什么都拦不住)
check("files_max=0 → text", tool_glyph(0, None) == "text")
check("files_max=0 with stray tag still text", tool_glyph(0, "image") == "text")

# 3) 文件工具 = tag 查表; 缺 tag fail loud 无兜底
for tag in _TAG_GLYPH:
    check(f"file tool tag '{tag}' → '{_TAG_GLYPH[tag]}'",
          tool_glyph(3, tag) == _TAG_GLYPH[tag])
try:
    tool_glyph(1, None)
    check("file tool missing tag → KeyError (fail loud)", False)
except KeyError:
    check("file tool missing tag → KeyError (fail loud)", True)

print(f"\n{'FAIL' if FAILED else 'PASS'}: tool icon tag-table contract"
      + (f" — {len(FAILED)} failed: {FAILED}" if FAILED else ""))
sys.exit(1 if FAILED else 0)
