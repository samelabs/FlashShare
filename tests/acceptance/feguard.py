# 前端结构门禁(固化): ①全部pages/*.js过node check ②模板零大内联 ③零jinja泄漏
import re, glob, subprocess, sys
fails = []
# 1. node check全部JS
for f in sorted(glob.glob('/opt/flashshare/static/pages/*.js')) + ['/opt/flashshare/static/app.js', '/opt/flashshare/static/i18n.js']:
    r = subprocess.run(['node', '--check', f], capture_output=True)
    if r.returncode:
        fails.append(f'node {f}: {r.stderr.decode()[:80]}')
# 2. 模板内联JS ≤ 500字符(数据注入豁免: base.html的__USER__块)
for f in sorted(glob.glob('/opt/flashshare/app/templates/**/*.html', recursive=True)):
    src = open(f).read()
    for m in re.findall(r'<script>(.*?)</script>', src, re.S):
        body = m.strip()
        if len(body) > 500 and 'user_json' not in body and '__PAGE__' not in body:
            fails.append(f'inline>{500} {f.split("templates/")[-1]} ({len(body)}c)')
# 3. JS零jinja
for f in glob.glob('/opt/flashshare/static/pages/*.js'):
    if re.search(r'\{\{[^}]*\}\}|\{%[^%]*%\}', open(f).read()):
        fails.append(f'jinja leak {f}')
print('\n'.join(fails) if fails else 'PASS: %d JS files checked, templates clean, zero jinja leak' % (len(glob.glob('/opt/flashshare/static/pages/*.js')) + 2))
sys.exit(1 if fails else 0)
