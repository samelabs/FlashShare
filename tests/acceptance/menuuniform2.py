# full-block window assertions (no brittle char offsets)
import re
css = open('/opt/flashshare/static/style.css').read()
row = re.search(r'\.user-dropdown a, \.user-dropdown button \{[^}]*\}', css).group(0)
svg = re.search(r'\.user-dropdown a svg[^{]*\{[^}]*\}', css).group(0)
checks = {
  "one row rule, a+button unified": len(re.findall(r'\.user-dropdown a,', css)) == 1,
  "row: padding 8px s3 uniform": 'padding: 8px var(--s3);' in row,
  "row: justify/min-height globals reset": 'justify-content: flex-start' in row and 'min-height: 0' in row,
  "svg 18px uniform": 'width: 18px; height: 18px;' in svg,
  "divider inside 16px gap": '.menu-sec + .menu-sec { border-top: 1px solid var(--line); margin-top: -1px; padding-top: 0.5px; }' in css,
  "first row of later groups 7.5px": '.menu-sec + .menu-sec > a:first-child { padding-top: 7.5px; }' in css,
  "menu-val muted": '.user-dropdown .menu-val { color: var(--muted); font-weight: 500;' in css,
  "no doubled selector prefix": '.user-dropdown .user-dropdown' not in css,
  "no transform on rows": 'transform' not in row,
}
ok = True
for k, v in checks.items():
    print(("PASS" if v else "FAIL"), k); ok = ok and v
print("ALL PASS" if ok else "HAS FAILURES")
