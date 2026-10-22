#!/usr/bin/env python3
"""Source-level architecture invariants (static; no stack needed).

Semantic guards that complement — never replace — the behavior suites:

  S1  orchestrator is staging-only: it neither writes upload records nor
      deletes Gokapi files (staged-result cleanup lives in gate.py).
  S2  the metering gate holds no secrets.
  S3  operation identity: production JS never sends `client_ref: null`;
      MCP tools/call declares client_ref required.
  S4  payment identity: the topup route rejects a malformed checkout
      request id, derives the order id from the request id, never marks
      an order failed on an unknown checkout-creation outcome, and the
      topup route / pricing page carry no tool-call client_ref; `UPDATE orders SET` lives only in
      repo/orders.py; ledger has exactly one `SET status='ok'` writer.
  S5  gate `_finish` settles from the commission snapshot, never the
      current commission config.
  S6  Jinja dict safety: templates never read `texts.<dict-method-name>`
      as an attribute (it resolves to the method); keys use ['key'].
"""
import os
import re
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name} — {str(detail)[:200]}")


def read(path):
    with open(os.path.join(REPO, path), encoding="utf-8") as f:
        return f.read()


def py_files(*roots):
    for root in roots:
        for d, _dirs, files in os.walk(os.path.join(REPO, root)):
            if "__pycache__" in d:
                continue
            for f in files:
                if f.endswith(".py"):
                    yield os.path.relpath(os.path.join(d, f), REPO)


# ── S1. orchestrator is staging-only ──
_orch = read("core/execution/orchestrator.py")
check("S1a orchestrator writes no upload record", "save_upload_record" not in _orch)
check("S1b orchestrator deletes no Gokapi file", "delete_gokapi_file" not in _orch)

# ── S2. gate holds no secrets ──
_gate = read("core/execution/gate.py")
check("S2 gate.py has no secrets access", "secrets" not in _gate)

# ── S3. operation identity ──
check("S3a production JS never sends client_ref: null",
      re.search(r"client_ref: *null", read("static/app.js")) is None)
check("S3b MCP tools/call declares client_ref required",
      'required = ["client_ref"]' in read("app/routes/mcp.py"))

# ── S4. payment identity / order writer ──
_topup = read("app/routes/topup.py")
check("S4a topup rejects a malformed checkout request id",
      "checkout_request_id_invalid" in _topup)
check("S4b topup derives the order id from the request id",
      "order_id_from_request_id" in _topup)
_after_checkout = [
    "\n".join(_topup.splitlines()[i:i + 9])
    for i, l in enumerate(_topup.splitlines()) if "create_credits_checkout" in l]
check("S4c checkout creation is never followed by mark_failed",
      _after_checkout and not any("mark_failed" in b for b in _after_checkout))
check("S4d topup route / pricing page carry no client_ref",
      "client_ref" not in _topup and "client_ref" not in read("static/pages/pricing.js"))
_order_writers = [p for p in py_files("app", "core")
                  if not p.endswith(os.path.join("repo", "orders.py"))
                  and "UPDATE orders SET" in read(p)]
check("S4e UPDATE orders SET only in repo/orders.py", not _order_writers, _order_writers)
check("S4f ledger has exactly one SET status='ok' writer",
      read("core/repo/ledger.py").count("SET status='ok'") == 1)

# ── S5. _finish settles from the snapshot ──
_fin = _gate.split("def _finish")[1].split("\ndef ")[0]
check("S5 gate._finish never reads the current commission config",
      "commission_rate()" not in _fin.replace("commission_rate_snapshot", ""))

# ── S6. Jinja dict safety ──
_bad = []
_attr = re.compile(r"texts\.(copy|items|keys|values|get|update|pop|clear|fromkeys|setdefault|popitem)(?![\w(])")
for d, _dirs, files in os.walk(os.path.join(REPO, "app", "templates")):
    for f in files:
        p = os.path.join(d, f)
        with open(p, encoding="utf-8", errors="ignore") as fh:
            for n, line in enumerate(fh, 1):
                if _attr.search(line):
                    _bad.append(f"{os.path.relpath(p, REPO)}:{n}")
check("S6 templates use texts['key'] for dict-method-named keys", not _bad, _bad)

# i18n 按需加载生成物: 首屏包(en) + static/i18n/<lang>.js 分片, 由
# SERVER_I18N 单源生成; --check 保证提交的生成物与源一致 (10 语言完整)。
_r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "gen_i18n.py"), "--check"],
                    capture_output=True, text=True)
check("S7 i18n artifacts match SERVER_I18N (first-screen pack + 10 shards)",
      _r.returncode == 0, (_r.stdout + _r.stderr).strip()[:200])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL or not PASS else 0)
