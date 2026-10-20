#!/usr/bin/env python3
"""Locale mechanism canonical — locale = the language tag of the text
itself. The publish form submits {主语言: 文本} with the main-language
key first; extra language rows patch other locales.

  L1  a tool stored with a str label/desc, edited via the Web patch
      header to ADD a locale, keeps the original text under the
      edit_locale key (merge base {edit_locale: old str}, not {}).
  L2  creating with a dict label stores it verbatim — the main
      language key stays FIRST (dict first key = original language).
  L3  params text leaves (label / placeholder / options[].label)
      submitted as {主语言: 文本} patch the old declaration —
      unmentioned locales survive the edit.
  L4  an empty submitted locale value deletes that locale key
      (tombstone) while the others stay untouched.
"""
import json as _json
import os as _os
import sys as _sys
import time as _time
import uuid as _uuid

_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required.\n"); _sys.exit(2)
try:
    if int(_TEST_PORT, 10) == 8000:
        _sys.stderr.write("ERROR: production port not allowed.\n"); _sys.exit(2)
except ValueError:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer.\n"); _sys.exit(2)
TEST_DB = _os.environ.get("TEST_DB_URL")
if not TEST_DB:
    _sys.stderr.write("ERROR: TEST_DB_URL is required.\n"); _sys.exit(2)

_REPO = _os.path.abspath(_os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), "..", ".."))
_sys.path.insert(0, _REPO)
_sys.path.insert(0, _os.path.join(_REPO, "core"))
_sys.path.insert(0, _os.path.join(_REPO, "app"))
_os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")
from tests.support.cleanup import purge_users  # noqa: E402

FAIL = []
COUNT = 0


def check(name, ok, detail=""):
    global COUNT
    COUNT += 1
    print(("✓" if ok else "✗"), name, ("— " + str(detail)[:160]) if (detail and not ok) else "")
    if not ok:
        FAIL.append(name)


from database import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app as _app  # noqa: E402
from app.web_auth import create_browser_session, COOKIE_NAME  # noqa: E402

client = TestClient(_app)
ts = int(_time.time())
_u = "bl" + _uuid.uuid4().hex[:6]


def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])


def _row(tool_key):
    with db() as con:
        return dict(con.execute(
            "select label_json, desc_json, params_json from platform_tools"
            " where tool_key = %s", (tool_key,)).fetchone())


_uid = _mkuser(_u)
_sess = create_browser_session(_uid)

def _create(label, desc=None, params=None):
    body = {
        "label": label, "url": "https://93.184.216.34/x", "timeout_s": 30,
        "files_min": 0, "files_max": 1, "format_families": "image",
        "accept": [".png"], "outputs": ["text"], "cost": 0,
    }
    if desc is not None:
        body["desc"] = desc
    if params is not None:
        body["params"] = params
    r = client.post("/api/usertools", json=body, cookies={COOKIE_NAME: _sess})
    assert r.status_code == 200, r.text
    return r.json()["tool_key"]


# ══════════ L1. str original survives a Web locale patch that adds ja ══════════
_k1 = _create("Locale Original", desc="Original description",
              params=[{"key": "style", "type": "enum", "required": False,
                       "label": "Style",
                       "options": [{"value": "a", "label": "Alpha"}]}])
# rewrite label/desc into the legacy str storage shape (rows created
# before dict normalization / operator-sealed str declarations) — the
# exact rows the {edit_locale: old str} merge base exists for
with db() as con:
    con.execute(
        "update platform_tools set label_json = %s, desc_json = %s"
        " where tool_key = %s",
        (_json.dumps("Locale Original"), _json.dumps("Original description"), _k1))
# the exact Web-form shape: main locale first with its unchanged text,
# the added locale as an extra row
r = client.patch(f"/api/usertools/{_k1}", json={
    "label": {"zh": "Locale Original", "ja": "ロケール原名"},
    "desc": {"zh": "Original description", "ja": "元の説明"},
    "params": [{"key": "style", "type": "enum", "required": False,
                "label": {"zh": "Style"},
                "options": [{"value": "a", "label": {"zh": "Alpha"}}]}],
}, headers={"X-518-Edit-Locale": "zh"}, cookies={COOKIE_NAME: _sess})
check("L1a Web patch (add ja) returns ok", r.status_code == 200 and r.json().get("ok"),
      f"{r.status_code} {r.text[:120]}")
_row1 = _row(_k1)
_lab1 = _json.loads(_row1["label_json"])
_desc1 = _json.loads(_row1["desc_json"])
check("L1b str label original preserved under edit_locale",
      _lab1 == {"zh": "Locale Original", "ja": "ロケール原名"}, _lab1)
check("L1c str desc original preserved under edit_locale",
      _desc1 == {"zh": "Original description", "ja": "元の説明"}, _desc1)
_par1 = _json.loads(_row1["params_json"])[0]
check("L1d str param leaf original preserved as {edit_locale: old str}",
      _par1["label"] == {"zh": "Style"},
      _par1["label"])
check("L1e enum option leaf locale-patched",
      _par1["options"][0]["label"] == {"zh": "Alpha"},
      _par1["options"][0]["label"])

# ══════════ L2. dict creation stores the main language key FIRST ══════════
_k2 = _create({"ja": "ツール名", "en": "Tool Name"}, desc={"ja": "説明", "en": "Desc"})
_lab2 = _json.loads(_row(_k2)["label_json"])
_desc2 = _json.loads(_row(_k2)["desc_json"])
check("L2a created label is the dict, main language key first",
      list(_lab2.keys()) == ["ja", "en"], _lab2)
check("L2b created desc is the dict, main language key first",
      list(_desc2.keys()) == ["ja", "en"], _desc2)

# ══════════ L3. params dict leaves patch, unmentioned locales survive ══════════
_k3 = _create("Param I18n", params=[{
    "key": "style", "type": "enum", "required": False,
    "label": {"en": "Style", "ja": "スタイル"},
    "options": [{"value": "a", "label": {"en": "Alpha", "ja": "アルファ"}}]},
])
r = client.patch(f"/api/usertools/{_k3}", json={
    "label": {"zh": "参数多语"},
    "params": [{"key": "style", "type": "enum", "required": False,
                "label": {"zh": "风格"},
                "options": [{"value": "a", "label": {"zh": "甲"}}]}],
}, headers={"X-518-Edit-Locale": "zh"}, cookies={COOKIE_NAME: _sess})
check("L3a params locale patch returns ok", r.status_code == 200 and r.json().get("ok"),
      f"{r.status_code} {r.text[:120]}")
_par3 = _json.loads(_row(_k3)["params_json"])[0]
check("L3b param label: zh patched, en+ja survive",
      _par3["label"] == {"en": "Style", "ja": "スタイル", "zh": "风格"},
      _par3["label"])
check("L3c option label: zh patched, en+ja survive",
      _par3["options"][0]["label"] == {"en": "Alpha", "ja": "アルファ", "zh": "甲"},
      _par3["options"][0]["label"])
_lab3 = _json.loads(_row(_k3)["label_json"])
check("L3d tool label dict base preserved, zh patched in",
      _lab3 == {"en": "Param I18n", "zh": "参数多语"}, _lab3)

# ══════════ L4. empty submitted locale = tombstone delete ══════════
r = client.patch(f"/api/usertools/{_k2}", json={
    "label": {"ja": "ツール名", "en": ""},
    "desc": {"ja": "説明", "en": ""},
}, headers={"X-518-Edit-Locale": "ja"}, cookies={COOKIE_NAME: _sess})
check("L4a tombstone patch returns ok", r.status_code == 200 and r.json().get("ok"),
      f"{r.status_code} {r.text[:120]}")
_row4 = _row(_k2)
check("L4b deleted locale gone, original (first key) preserved",
      _json.loads(_row4["label_json"]) == {"ja": "ツール名"},
      _row4["label_json"])
check("L4c desc tombstone deletes only that locale",
      _json.loads(_row4["desc_json"]) == {"ja": "説明"},
      _row4["desc_json"])

# ── cleanup ──
with db() as con:
    con.execute("delete from platform_tools where tool_key in %s",
                ((_k1, _k2, _k3),))
    con.execute("delete from pricing where tool_id in %s",
                ((_k1, _k2, _k3),))
    purge_users(con, [_uid])

print(f"\n{COUNT - len(FAIL)}/{COUNT} passed")
_sys.exit(1 if FAIL else 0)
