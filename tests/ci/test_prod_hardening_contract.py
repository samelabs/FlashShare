#!/usr/bin/env python3
"""Prod-hardening ops/UI contracts — source + sandbox behavior locks.

  C1  operator takedown toast states the real semantics (public link
      deleted; storage deletion submitted; audited).
  C2  backup unit reads /etc/flashshare.env (EnvironmentFile), carries no
      hard-coded empty FLASHSHARE_BACKUP_REMOTE, and runs UMask=0077.
  C3  backup.sh: umask 077; FLASHSHARE_BACKUP_REMOTE parsed from the same
      env file as DATABASE_URL (no second source); no rclone `|| true`.
  C4  backup.sh sandbox run (fake date=Sunday, fake rclone failing ONLY
      the weekly destination): non-zero exit naming the weekly failure;
      control run (rclone always ok) exits 0; created files are 0600.
  C5  DEVELOPMENT.md nginx block: X-Real-IP set from $remote_addr in
      every proxy location, Cloudflare real-IP restoration, baseline
      security response headers (nosniff / Referrer-Policy / XFO).
  C6  publish page credential-clear wiring: 10-language i18n key, edit
      mode reveals the remove-auth checkbox, submit sends both empty
      strings when checked.
"""
import os
import stat
import subprocess
import sys
import tempfile

_REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(_REPO, "core"))

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
    with open(os.path.join(_REPO, path), encoding="utf-8") as f:
        return f.read()


# ── C1. operator takedown toast wording ──
_js = read("static/pages/samelabs_content.js")
check("C1a takedown toast states the submitted-deletion semantics",
      "公开链接已删除，存储文件已提交删除；已写审计" in _js)
check("C1b old over-promising wording gone",
      "已删除文件与物理存储" not in _js)

# ── C2. backup systemd unit ──
_unit = read("scripts/flashshare-backup.service")
check("C2a unit loads the shared env file",
      "EnvironmentFile=-/etc/flashshare.env" in _unit)
check("C2b unit has no hard-coded FLASHSHARE_BACKUP_REMOTE",
      "Environment=FLASHSHARE_BACKUP_REMOTE" not in _unit)
check("C2c unit enforces UMask=0077", "UMask=0077" in _unit)

# ── C3. backup.sh source contracts ──
_sh = read("scripts/backup.sh")
check("C3a script sets umask 077", "umask 077" in _sh)
check("C3b FLASHSHARE_BACKUP_REMOTE parsed from the env file "
      "(no process-env default left)",
      'env_value FLASHSHARE_BACKUP_REMOTE' in _sh
      and '${FLASHSHARE_BACKUP_REMOTE:-}' not in _sh)
_rclone_swallow = [ln.strip() for ln in _sh.splitlines()
                   if "rclone copy" in ln and "|| true" in ln]
check("C3c no rclone copy line ends in || true (weekly failures visible)",
      not _rclone_swallow, _rclone_swallow)
check("C3c2 weekly rclone legs fail() like the daily ones",
      'fail "rclone weekly pg upload failed"' in _sh
      and 'fail "rclone weekly gokapi upload failed"' in _sh)

# ── C4. backup.sh sandbox behavior ──
_td = tempfile.mkdtemp(prefix="ph-backup-")
_bin = os.path.join(_td, "bin")
_root = os.path.join(_td, "backups")
_gk_data = os.path.join(_td, "gkdata")
_gk_cfg = os.path.join(_td, "gkcfg")
_envf = os.path.join(_td, "flashshare.env")
os.makedirs(_bin)
os.makedirs(_root)
os.makedirs(_gk_data)
os.makedirs(_gk_cfg)
with open(_envf, "w", encoding="utf-8") as f:
    f.write("DATABASE_URL=postgresql://u:p@127.0.0.1:5432/db\n"
            "FLASHSHARE_BACKUP_REMOTE=phb:bucket\n")


def _fake(name, body):
    p = os.path.join(_bin, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(p, 0o755)


# fake date: DOW=7 (Sunday); stable stamp
_fake("date", 'case "$*" in *%u*) echo 7;; *) echo 20260927T043000Z;; esac')
_fake("pg_dump", "exit 0")
_fake("python3", 'touch "$3"')               # argv: - SRC DST
_fake("tar", 'touch "$2"')                  # argv: -czf FILE ...


def _run(mode):
    # mode "ok": rclone always succeeds (recording what was uploaded);
    # "weekly-fails": only the weekly destination fails — proving the
    # SUNDAY branch's failure handling specifically; "sums-fails": only
    # the checksum-manifest upload fails.
    _fake("rclone", (
        'echo "$*" >> "$PHB_RCLONE_LOG"\nexit 0' if mode == "ok" else
        'for a in "$@"; do case "$a" in *weekly*) exit 1;; esac; done\nexit 0'
        if mode == "weekly-fails" else
        'for a in "$@"; do case "$a" in *SHA256SUMS*) exit 1;; esac; done\n'
        'exit 0'))
    env = dict(os.environ)
    env.update({
        "PATH": _bin + os.pathsep + env["PATH"],
        "FLASHSHARE_BACKUP_ROOT": _root,
        "FLASHSHARE_ENV_FILE": _envf,
        "FLASHSHARE_GOKAPI_DATA": _gk_data,
        "FLASHSHARE_GOKAPI_CONFIG": _gk_cfg,
        "PHB_RCLONE_LOG": os.path.join(_td, "rclone.log"),
    })
    env.pop("FLASHSHARE_BACKUP_REMOTE", None)
    p = subprocess.run(["bash", os.path.join(_REPO, "scripts", "backup.sh")],
                       capture_output=True, text=True, env=env, timeout=120)
    return p.returncode, p.stdout + p.stderr


import shutil as _shutil
_rc, _out = _run("ok")
_pg = os.path.join(_root, "daily", "pg-20260927T043000Z.dump")
check("C4a control run (rclone ok, Sunday) exits 0", _rc == 0, f"{_rc} {_out[-300:]}")
check("C4b backup files created 0600 (umask 77)",
      os.path.exists(_pg) and stat.S_IMODE(os.stat(_pg).st_mode) == 0o600,
      oct(os.stat(_pg).st_mode & 0o777) if os.path.exists(_pg) else "missing")
with open(os.path.join(_td, "rclone.log"), encoding="utf-8") as _f:
    _up = _f.read()
# each log line: rclone copy <src> <remote>/daily|weekly/ --log-file=...
# log line: "copy <src> <remote>/<dir>/ --log-file=..." — src is token 1
_up_d = sorted(os.path.basename(ln.split()[1])
               for ln in _up.splitlines() if "/daily/" in ln)
_up_w = sorted(os.path.basename(ln.split()[1])
               for ln in _up.splitlines() if "/weekly/" in ln)
_STAMP = "20260927T043000Z"
check("C4e remote daily carries the full triple (pg + gokapi + SHA256SUMS)",
      _up_d == [f"SHA256SUMS-{_STAMP}", f"gokapi-{_STAMP}.tar.gz",
                f"pg-{_STAMP}.dump"], str(_up_d))
check("C4f remote weekly carries the full triple too",
      _up_w == [f"SHA256SUMS-{_STAMP}", f"gokapi-{_STAMP}.tar.gz",
                f"pg-{_STAMP}.dump"], str(_up_w))

_shutil.rmtree(_root, ignore_errors=True)
os.makedirs(_root, exist_ok=True)
_rc, _out = _run("weekly-fails")
check("C4c Sunday run with weekly-destination rclone failure exits non-zero",
      _rc != 0, f"{_rc} {_out[-300:]}")
check("C4d the failure names the weekly upload",
      "rclone weekly pg upload failed" in _out, _out[-300:])

_shutil.rmtree(_root, ignore_errors=True)
os.makedirs(_root, exist_ok=True)
_rc, _out = _run("sums-fails")
check("C4g checksum-manifest upload failure exits non-zero",
      _rc != 0, f"{_rc} {_out[-300:]}")
check("C4h the failure names the checksums upload",
      "rclone daily checksums upload failed" in _out, _out[-300:])
_shutil.rmtree(_td, ignore_errors=True)

# ── C5. DEVELOPMENT.md nginx contracts ──
_doc = read("DEVELOPMENT.md")
check("C5a every proxy location derives X-Real-IP from $remote_addr",
      _doc.count("proxy_set_header X-Real-IP $remote_addr;") == 3)
check("C5b Cloudflare real-IP restoration configured",
      "real_ip_header CF-Connecting-IP;" in _doc
      and _doc.count("set_real_ip_from ") >= 20
      and "173.245.48.0/20" in _doc and "2c0f:f248::/32" in _doc)
check("C5c baseline security headers present",
      "add_header X-Content-Type-Options nosniff always;" in _doc
      and "add_header Referrer-Policy strict-origin-when-cross-origin always;" in _doc
      and "add_header X-Frame-Options SAMEORIGIN always;" in _doc)

# ── C6. publish page credential-clear wiring ──
from i18n import SERVER_I18N  # noqa: E402
_missing = [lg for lg, kv in SERVER_I18N.items()
            if not kv.get("pub_remove_auth")]
check("C6a pub_remove_auth exists in every supported language",
      not _missing and len(SERVER_I18N) == 10, _missing)
_gen = read("static/i18n.js")
check("C6b generated i18n.js carries the key", "pub_remove_auth" in _gen)
_html = read("app/templates/account/publish.html")
check("C6c edit-mode remove-auth checkbox exists and starts hidden",
      '<label class="pub-wide hidden" id="pubRemoveAuthRow">' in _html
      and 'id="fRemoveAuth"' in _html
      and 'data-i18n="pub_remove_auth"' in _html)
_pub = read("static/pages/publish.js")
check("C6d refill reveals the checkbox only for a stored credential",
      'classList.toggle("hidden", !t.has_auth)' in _pub)
check("C6e checked box submits both empty strings (server clear form)",
      'auth_header: removeAuth ? ""' in _pub
      and 'auth_token: removeAuth ? ""' in _pub)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
