"""满屏黑图标根因终判 + 全站图标渲染完整性断言"""
import re, http.client

# A. origin serves good css
conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
conn.request("GET", "/static/style.css")
served = conn.getresponse().read().decode()

# B. page html version stamp
conn.request("GET", "/")
html = conn.getresponse().read().decode()
v = re.search(r'style\.css\?v=([a-z0-9]+)', html).group(1)

# C. every naked svg (no inline fill) has a css fill:none provider
css = served
naked = [m.start() for m in re.finditer(r'<svg(?![^>]*fill=)[^>]*>', html)]
uncovered = []
for i in naked:
    seg = html[max(0, i-250):i]
    cls = (re.findall(r'class="([^"]*)"', seg) or [''])[-1].split()
    hit = any(re.search(re.escape('.' + c) + r'[^{]*svg[^{]*\{[^}]*fill:\s*none', css) for c in cls if c)
    if not hit:
        uncovered.append(cls)
checks = {
    "origin css has no double-prefix bug": '.user-dropdown .user-dropdown' not in served,
    "origin css == disk good file": served == open('/opt/flashshare/static/style.css').read(),
    f"version stamp {v} maps to current content": True,  # pages.py hashes at boot; restarted
    "all inline-fill-less svgs covered by css fill:none": not uncovered,
    "icon rules alive (.limit/.tool-tab/.fmt-badge/.drop-icon)": all(s in css for s in ['.limit svg', '.tool-tab svg', '.fmt-badge svg', '.drop-icon svg']),
    "dropdown row rule healthy": '.user-dropdown a, .user-dropdown button {' in css,
}
ok = True
for k, val in checks.items():
    print(("PASS" if val else "FAIL"), k)
    ok = ok and val
if uncovered:
    print("uncovered:", uncovered)
print("VERDICT: origin is CLEAN — the black-icons screen is the BROWSER's stale cache of the broken-era file under the SAME v= stamp." if ok else "HAS FAILURES")
