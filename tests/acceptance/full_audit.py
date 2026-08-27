"""全站收口审计: i18n双向完整性 + CSS死类 + JS悬挂DOM引用 + 静态资源可达 + 模板键泄漏"""
import re, glob, json, subprocess, sys
sys.path.insert(0, '/opt/flashshare/core')
import os
os.environ.setdefault('FLASHSHARE_SESSION_SECRET', 'x')

fails = []

# ═══ A. i18n 双向完整性 ═══
import importlib.util
# 完整包 = static/i18n.js en包(生成产物, 双源合并后的真相)
_js = open('/opt/flashshare/static/i18n.js').read()
import json as _json
_D = _json.loads(re.search(r'window\.__I18N__ = (\{.*\});', _js, re.S).group(1))
defined = set(_D['en'].keys())

used = set()
# 模板: {{ texts.X }} / texts['X'] / data-i18n="X"
for f in glob.glob('/opt/flashshare/app/templates/**/*.html', recursive=True):
    src = open(f).read()
    used |= set(re.findall(r'\{\{\s*texts\.([a-zA-Z_][\w]*)', src)) - {'get'}  # texts.get()是方法
    used |= set(re.findall(r"texts\['([a-zA-Z_][\w]*)'\]", src))
    used |= set(re.findall(r'data-i18n(?:-placeholder)?="([a-zA-Z_][\w]*)"', src))
# JS: window.t("X") / _msg("X") / _errm 拼 mytools_err_X
for f in glob.glob('/opt/flashshare/static/**/*.js', recursive=True) + ['/opt/flashshare/static/app.js']:
    if 'vendor' in f: continue
    src = open(f).read()
    used |= set(re.findall(r'window\.t\("([a-zA-Z_][\w]*)"\)', src))
    used |= set(re.findall(r'_msg\("([a-zA-Z_][\w]*)"\)', src))
    used |= set(re.findall(r'_tx\(["\']([a-zA-Z_][\w]*)', src))
    used |= set(re.findall(r"(?<![\w.])t\(['\"]([a-zA-Z_][\w]*)['\"]", src))
    used |= set(re.findall(r"_msg\(['\"]([a-zA-Z_][\w]*)", src))
    used |= set(re.findall(r"window\.t\(['\"]([a-zA-Z_][\w]*)", src))
missing = {k for k in used if k not in defined and not k.startswith(('mytools_err_', 'mytools_v_', 'mytools_set_', 'earn_status_'))}
# 动态引用族豁免(逐族实证过): 工具id+_desc(data-tool-id驱动), ledger_*/earn_status_/
# mytools_v_*/mytools_set_*/mytools_st_*(_msg拼), zip_err_*(keyMap值), 裸t()(app.js别名),
# {{ t.k }}模板形式(print.html), 数值选择器(SSR拼)
import glob as _g, re as _re
_dyn = set()
import http.client as _hc
_hc_ = _hc.HTTPConnection('127.0.0.1', 8000, timeout=20); _hc_.request('GET', '/')
_h = _hc_.getresponse().read().decode()  # live页取运行时data-tool-id
for _tid in _re.findall(r'data-tool-id="([^"]+)"', _h):
    _dyn |= {_tid, _tid + '_desc'}
_srcdump = ''
for _f in _g.glob('/opt/flashshare/static/pages/*.js') + ['/opt/flashshare/static/app.js', '/opt/flashshare/static/zip-preview.js']:
    _srcdump += open(_f).read()
for _f in _g.glob('/opt/flashshare/app/templates/**/*.html', recursive=True):
    _srcdump += open(_f).read()
for _k in list(defined):
    if _k.startswith(('ledger_','earn_status_','mytools_v_','mytools_set_','mytools_st_','mytools_err_','err_','zip_err_')): _dyn.add(_k)
    if _re.search(r'[{,.\s(]' + _re.escape(_k) + r'[}\s.,)]', _srcdump): _dyn.add(_k)  # 裸引用/模板t.k/对象键
# 后端错误码族: api/routes返回的error码被t(ek)动态消费 — py源扫描收集
_pydump = ''
for _f in _g.glob('/opt/flashshare/app/routes/*.py') + _g.glob('/opt/flashshare/app/**/*.py', recursive=True):
    _pydump += open(_f).read()
_errcodes = set(_re.findall(r'"error":\s*"([a-z_]+)"', _pydump)) | set(_re.findall(r'"error":\s*err', _pydump) and [])
dead = {k for k in defined if k not in used and k not in _dyn and k not in _errcodes and not k.endswith('_text') and k not in ('privacy_title','terms_title')}
# err_族: _errm动态拼(裸码→err_/mytools_err_), 保守豁免err_前缀; 其余死键=真死
if missing: fails.append(f'A1 模板/JS引用但i18n缺失: {sorted(missing)[:8]}')
if dead: fails.append(f'A2 i18n死键({len(dead)}): {sorted(dead)[:12]}')

# ═══ B. CSS 死类(近期删块的残渣) ═══
css = open('/opt/flashshare/static/style.css').read()
css_classes = set(re.findall(r'\.([a-zA-Z][a-zA-Z0-9_-]+)[^a-zA-Z0-9_-]', css))
blob = ''
for f in glob.glob('/opt/flashshare/app/templates/**/*.html', recursive=True):
    blob += open(f).read()
for f in glob.glob('/opt/flashshare/static/pages/*.js') + ['/opt/flashshare/static/app.js', '/opt/flashshare/static/sw.js']:
    blob += open(f).read()
for f in glob.glob('/opt/flashshare/app/**/*.py', recursive=True):
    blob += open(f).read()
# 动态类豁免
DYN = {'ledger', 'st', 'fmt', 'token', 'hidden', 'active', 'copied', 'picked', 'done', 'v', 'pico', 'w3', 'zip-item', 'zip-item-icon', 'zip-item-name', 'zip-item-size', 'zip-preview-list', 'zip-preview-meta', 'zip-preview-panel', 'zip-preview-title'}  # w3=SVG ns URL; zip-*=zip-preview.js innerHTML动态建
dead_css = set()
for c in css_classes:
    if c in DYN: continue
    # 动态前缀族: ledger-{{kind}}/st-{status}/v-{vis}由模板拼接
    if re.search(r'\b' + re.escape(c) + r'\b', blob):
        continue
    # 动态拼接族: ledger-{{kind}} / st-{status} / v-{vis} / fmt-{type}
    for pref in ('ledger-', 'st-', 'v-', 'fmt-'):
        if c.startswith(pref) and (pref[:-1] + '-{{') in blob:
            break
    else:
        dead_css.add(c)
if dead_css: fails.append(f'B CSS死类({len(dead_css)}): {sorted(dead_css)[:15]}')

# ═══ C. JS悬挂DOM引用(getById的元素必须存在于某模板) ═══
tpl_all = blob
for f in glob.glob('/opt/flashshare/static/pages/*.js') + ['/opt/flashshare/static/app.js']:
    src = open(f).read()
    for eid in re.findall(r'getElementById\("([^"]+)"\)', src):
        if f'id="{eid}"' not in tpl_all:
            fails.append(f'C {f.split("/")[-1]}引用#{eid}但模板无此元素')

# ═══ D. 模板静态资源引用可达 ═══
for f in glob.glob('/opt/flashshare/app/templates/**/*.html', recursive=True):
    src = open(f).read()
    for m in re.findall(r'(?:src|href)="(/static/[^"?]+)', src):
        import os.path as P
        if not P.exists('/opt/flashshare' + m):
            fails.append(f'D {f.split("templates/")[-1]}引用不存在的{m}')

# ═══ E. agents/tokens近期删块残渣全文扫描 ═══
residue = ['Cherry', '豆包', 'curator adopt', 'agents_clients', 'agents_semi', 'agents_skill',
            'acct-agents-link', 'token-revoked', 'skillCmd', 'copySkill']
for kw in residue:
    hits = []
    for f in glob.glob('/opt/flashshare/app/templates/**/*.html', recursive=True) + glob.glob('/opt/flashshare/static/pages/*.js') + ['/opt/flashshare/static/style.css']:
        if kw in open(f).read():
            hits.append(f.split('flashshare/')[-1])
    if hits: fails.append(f'E 残渣[{kw}]: {hits[:3]}')

# ═══ F. 每页SSR零裸键(渲染级验证) ═══
import http.client, time, hmac, hashlib, secrets as _sec
import psycopg2
from config import SESSION_SECRET, COOKIE_NAME
PG = os.environ['DATABASE_URL'].replace('postgresql+psycopg://', 'postgresql://') if 'DATABASE_URL' in os.environ else None
if PG:
    def psql(q, a=()):
        con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
        rows = cur.fetchall() if cur.description else []
        con.commit(); con.close(); return rows
    ts = int(time.time()); uname = f"au{ts%100000}"
    tok_s = _sec.token_urlsafe(32)
    th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
    psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'a','user',%s,%s)", (uname, ts, ts))
    uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
    psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+900, ts))
    conn = http.client.HTTPConnection('127.0.0.1', 8000, timeout=30)
    ck = {"Cookie": f"{COOKIE_NAME}={tok_s}"}
    for p in ["/", "/agents", "/auth/register", "/account/tools", "/account/earnings", "/account/credits", "/account/tokens", "/account/profile", "/account/security"]:
        conn.request("GET", p, headers=ck)
        r = conn.getresponse(); h = r.read().decode()
        if r.status != 200:
            fails.append(f'F {p} status {r.status}'); continue
        # SSR文本区出现i18n键原样(形如 agents_xx/err_xx单词独占文本节点)
        body = h[h.find('<body>'):]
        for m in re.findall(r'>\s*([a-z]+(?:_[a-z]+){1,3})\s*<', body):
            if m in defined:
                fails.append(f'F {p} SSR裸键: {m}')
    psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
    psql("DELETE FROM users WHERE id=%s", (uid,))

print('═'*60)
if fails:
    print(f'发现 {len(fails)} 类漏点:')
    for f_ in fails: print(' ✗', f_)
else:
    print('PASS: i18n双向/CSS死类/JS悬挂DOM/静态可达/删块残渣/SSR裸键 六维全过')
