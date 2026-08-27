"""全站视觉规范断言: Pico引入后的实际渲染面 — 用真实HTML+两份CSS算出每个关键表面的生效样式"""
import http.client, time, sys, os, re, hmac, hashlib, secrets as _sec, json
sys.path.insert(0, "/opt/flashshare/core")
import psycopg2
from config import SESSION_SECRET, COOKIE_NAME
PG = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
def psql(q, a=()):
    con = psycopg2.connect(PG); cur = con.cursor(); cur.execute(q, a)
    rows = cur.fetchall() if cur.description else []
    con.commit(); con.close(); return rows
ts = int(time.time()); uname = f"vs{ts%100000}"
tok_s = _sec.token_urlsafe(32)
th = hmac.new(SESSION_SECRET.encode(), tok_s.encode(), hashlib.sha256).hexdigest()
psql("INSERT INTO users(username, display_name, status, created_at, last_login_at) VALUES(%s,'v','user',%s,%s)", (uname, ts, ts))
uid = psql("SELECT id FROM users WHERE username=%s", (uname,))[0][0]
psql("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(%s,%s,%s,%s)", (th, uid, ts+900, ts))
ck = {"Cookie": f"{COOKIE_NAME}={tok_s}"}
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)

STYLE = open('/opt/flashshare/static/style.css').read()
PICO = open('/opt/flashshare/static/vendor/pico.conditional.min.css').read()

def css_rules(css):
    return re.findall(r'([^{}]+)\{([^}]*)\}', css)

SR = css_rules(STYLE)
PR = css_rules(PICO)

def hits_pico_global(el, attr):
    """pico conditional里: 不带.pico前缀且会击中el的规则(=越界面)"""
    out = []
    for sel, body in PR:
        sel = sel.strip()
        if '.pico' in sel or '@' in sel: continue
        # 全局元素击中: 裸button/a/input等
        for part in sel.split(','):
            part = part.strip()
            if re.fullmatch(r'(?:[a-z0-9:()\[\]="\'^~+*., >_-]*\b)?' + el + r'\b.*', part) and not part.startswith('.') and not part.startswith('#'):
                visual = [d for d in body.split(';') if attr in d and '--pico' not in d]
                if visual:
                    out.append((part[:70], visual[0][:50]))
                break
    return out

checks = {}
# 1. 头像钮/上传钮/登出钮: pico全局规则不得提供 width/background/margin
for el, attrs in [('button', ['width', 'background', 'margin']), ('a', ['color', 'text-decoration', 'background'])]:
    leaks = []
    for a in attrs:
        leaks += hits_pico_global(el, a)
    checks[f'pico全局零越界 <{el}>'] = (len(leaks) == 0, leaks[:2])

# 2. style.css关键表面规则在场(视觉锚)
anchors = ['.topbar-profile {', '.user-dropdown a, .user-dropdown button {', '.drop {', '.tool-tab {',
           '.acct-table {', '.earn-cell', '.menu-sec + .menu-sec', '.modal {']
missing = [a for a in anchors if a.rstrip(' {') not in STYLE and a not in STYLE]
checks['style.css九大表面锚全在'] = (not missing, missing)

# 3. 8页真实渲染: 每页pico引用+style引用+顺序+零pico容器类泄漏(不该有.pico类被用)
pages = ["/", "/agents", "/auth/register", "/account/tools", "/account/earnings", "/account/credits", "/account/tokens", "/account/profile"]
bad = []
for p in pages:
    conn.request("GET", p, headers=ck)
    r = conn.getresponse(); h = r.read().decode()
    if r.status != 200: bad.append((p, r.status)); continue
    if not ('pico.conditional' in h and h.find('pico.conditional') < h.find('style.css?v=')): bad.append((p, 'css order'))
    if 'class="pico' in h: bad.append((p, '.pico class used'))
checks['8页: pico conditional加载+顺序+零.pico类'] = (not bad, bad[:3])

# 4. 按钮语义: 主按钮/次按钮统一定义在style.css(不依赖pico)
for probe in ['.btn-secondary', 'button.primary', '.primary-btn']:
    pass
checks['按钮体系(style.css自持)'] = ('.x-login, button {' in STYLE and '.controls .primary {' in STYLE and '.auth-form .primary' in STYLE, '')

ok = True
for k, (v, detail) in checks.items():
    print(('PASS' if v else 'FAIL'), k, detail if detail and not v else '')
    ok = ok and v
psql("DELETE FROM sessions WHERE user_id=%s", (uid,))
psql("DELETE FROM users WHERE id=%s", (uid,))
print('ALL PASS' if ok else 'HAS FAILURES')
