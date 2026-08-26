"""i18n error-key discipline probe: every backend error code must resolve
to a human message in ALL 10 languages — zero raw-key exposure."""
import sys, os, re, glob
sys.path.insert(0, "/opt/flashshare/core")
os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "x")
from i18n import SERVER_I18N

LANGS = list(SERVER_I18N.keys())

# 1. harvest every error code the backend can emit (all route files)
codes = set()
for f in glob.glob("/opt/flashshare/app/routes/*.py"):
    src = open(f).read()
    codes |= set(re.findall(r'"error":\s*"([a-z_0-9]+)"', src))
    codes |= set(re.findall(r'return None, "([a-z_0-9]+)"', src))

# 2. resolver contract mirrors _errm:
#    err_-prefixed → look up as-is; bare code → mytools_err_<code> (tools) or err_<code> (money)
def resolve(lang, code):
    T = SERVER_I18N[lang]
    if code.startswith("err_"):
        return T.get(code)
    for cand in (f"mytools_err_{code}", f"err_{code}"):
        if cand in T:
            return T[cand]
    return None

missing = []
for code in sorted(codes):
    for lang in LANGS:
        if resolve(lang, code) is None:
            missing.append(f"{code}@{lang}")

print(f"backend error codes: {len(codes)}; langs: {len(LANGS)}")
if missing:
    print(f"MISSING ({len(missing)}):")
    for m in missing[:20]:
        print("  ", m)
else:
    print("ALL codes resolve in ALL 10 languages — zero raw-key exposure")

# 3. JS side: no remaining bare alert(x.error) in account templates
leaks = []
for f in glob.glob("/opt/flashshare/app/templates/account/*.html"):
    src = open(f).read()
    for m in re.finditer(r"alert\(\s*[a-z0-9]+\.error\s*\)", src):
        leaks.append(f"{f.split('/')[-1]}: {m.group(0)}")
print("bare alert(code) left in templates:", leaks or "NONE")
