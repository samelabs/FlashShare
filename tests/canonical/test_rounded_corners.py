"""Rounded corners tool — user-chain guards (hosted southbound form).

The tool is no longer a first-party registry entry: it lives on the user
chain under owner systest548d51 with execution=http against the hosted
southbound surface /tool-endpoints/rounded-corners. Guards assert BOTH
halves of that reality: the pure handler module, and the southbound
endpoint contract. No DB, no HTTP — pure source assertions.
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
    "from tools import ToolError",
)

check("RC1 handler 存留 (register 已退场, 用户链形态)",
      "def rounded_corners_handler" in _src and "register(" not in _src)
check("RC2 radius 只允许三档 preset (无自由 px 通道)",
      '"small": 0.04' in _src and '"medium": 0.10' in _src and '"large": 0.20' in _src
      and 'radius_px' not in _src)
check("RC3 透明背景强制 alpha 格式 (JPEG/BMP → PNG 重编码)",
      "_ALPHA_FMTS" in _src and 'fmt, out_ext = "PNG", "png"' in _src)
check("RC4 白底允许 JPEG 保持 (quality=90)",
      'background == "white"' in _src and '"JPEG", quality=90' in _src)
check("RC5 radius 物理钳制 (不超过短边一半)",
      "min(r, min(w, h) // 2)" in _src)
check("RC6 import 面白名单 (禁 gokapi/db/网络)",
      _import_lines == _ALLOWED_IMPORTS)

_ht = open(_os.path.join(_repo, "app", "routes", "hosted_tools.py"), encoding="utf-8").read()
check("RC7 托管 southbound 面存在 (/tool-endpoints/rounded-corners)",
      '@router.post("/rounded-corners")' in _ht
      and "prefix=\"/tool-endpoints\"" in open(
          _os.path.join(_repo, "app", "main.py"), encoding="utf-8").read())
check("RC8 端点走签名拉取闸 (verify_fetch_token, 无旁路)",
      "_pull_signed_source" in _ht and "verify_fetch_token" in _ht)
check("RC9 端点要求 dispatch-minted hosted capability (非 Bearer)",
      "verify_hosted_exec_token" in _ht
      and 'via != "token"' not in _ht
      and 'auth_via' not in _ht)
_route_body = _ht[_ht.find("async def hosted_rounded_corners"):]
check("RC9b capability 验证先于 source 消费 (verify 在 pull 之前)",
      0 <= _route_body.find("verify_hosted_exec_token")
      < _route_body.find("_pull_signed_source"))
check("RC10 端点输出 data_b64 (json mode 合同)",
      '"data_b64"' in _ht)
check("RC11 无旧 /api/v1 namespace 残留 (无 alias)",
      "/api/v1" not in _ht
      and not _os.path.exists(_os.path.join(_repo, "app", "routes", "apiv1.py")))

print()
print("FAIL: " + (", ".join(_FAIL) if _FAIL else "无 — 全绿"))
_sys.exit(1 if _FAIL else 0)
