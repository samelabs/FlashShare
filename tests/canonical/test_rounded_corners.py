"""Rounded corners tool — declaration guards (registry single source).

Same guard family as other first-party tools: declaration facts must hold in
code. No DB, no HTTP — pure source assertions.
"""
import os as _os
import sys as _sys

_FAIL = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _FAIL.append(name)


_repo = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))

_src = open(_os.path.join(_repo, "core", "tools_corners.py"), encoding="utf-8").read()
_import_lines = tuple(
    line.strip()
    for line in _src.splitlines()
    if line.startswith(("import ", "from "))
)

_ALLOWED_IMPORTS = (
    "import io",
    "import os",
    "from PIL import Image, ImageDraw",
    "from tools import register, TYPE_PRODUCE, ToolError",
)

check("RC1 tool_id 是 rounded_corners (裸 id = 首方 official)",
      'tool_id="rounded_corners"' in _src)
check("RC2 radius 只允许三档 preset (无自由 px 通道)",
      '"small": 0.04' in _src and '"medium": 0.10' in _src and '"large": 0.20' in _src
      and 'radius_px' not in _src)
check("RC3 透明背景强制 alpha 格式 (JPEG/BMP → PNG 重编码)",
      "_ALPHA_FMTS" in _src and 'fmt, out_ext = "PNG", "png"' in _src)
check("RC4 白底允许 JPEG 保持 (quality=90)",
      'background == "white"' in _src and '"JPEG", quality=90' in _src)
check("RC5 radius 物理钳制 (不超过短边一半)",
      "min(r, min(w, h) // 2)" in _src)
check("RC6 import 面白名单 (禁 gokapi/db/网络)", _import_lines == _ALLOWED_IMPORTS)
check("RC7 params 声明 enum 类型 + default",
      '"type": "enum"' in _src and '"default": "medium"' in _src
      and '"default": "transparent"' in _src)
check("RC8 文件名 tools_corners.py (load_all 自动注册约定)",
      _os.path.basename(_os.path.join(_repo, "core", "tools_corners.py")) == "tools_corners.py")

print()
print("FAIL: " + (", ".join(_FAIL) if _FAIL else "无 — 全绿"))
_sys.exit(1 if _FAIL else 0)
