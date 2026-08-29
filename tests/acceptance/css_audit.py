"""视觉系统健康度盘点: 组件清单+CSS债检测（重复/死规则/裸类/命名散乱）"""
import re, glob
from collections import Counter

CSS = open('/opt/flashshare/static/518.css').read()

print("=" * 64)
print("A. CSS 规则健康度")
print("=" * 64)

# 1. 重复 selector（完全同名规则>1 = 债）
sels = re.findall(r'(?m)^([.#][a-zA-Z][^{},\n]*)\{', CSS)
# collapse whitespace for comparison
norm = [re.sub(r'\s+', ' ', s.strip()) for s in sels]
dupes = {k: v for k, v in Counter(norm).items() if v > 1}
print(f"1. 完全重复的 selector: {len(dupes)}")
for k in list(dupes)[:8]:
    print(f"   x{dupes[k]}  {k[:70]}")

# 2. 死规则（CSS 有但模板零引用的 class）
used = set()
for f in glob.glob('/opt/flashshare/app/templates/**/*.html', recursive=True):
    used |= set(re.findall(r'class="([^"]+)"', open(f).read()))
    for m in re.findall(r"classList\.(?:add|toggle|remove)\('([^']+)'", open(f).read()):
        used.add(m)
    for m in re.findall(r"className = '([^']+)'", open(f).read()):
        used.add(m)
used_flat = set()
for u in used:
    used_flat |= set(u.split())
css_classes = set(re.findall(r'\.([a-zA-Z][a-zA-Z0-9_-]+)', CSS))
dead = sorted(c for c in css_classes - used_flat if not c.startswith('st-') and c not in
              ('active','hidden','done','pos','neg','grow','off','mono','ta-r','community'))
print(f"\n2. 疑似死 class（CSS 定义但无模板引用）: {len(dead)}")
for c in dead[:15]:
    print(f"   .{c}")

# 3. 括号平衡
depth = 0; bad = 0
for ch in CSS:
    if ch == '{': depth += 1
    elif ch == '}':
        depth -= 1
        if depth < 0: bad += 1; depth = 0
print(f"\n3. 括号平衡: final={depth} negatives={bad}")

print()
print("=" * 64)
print("B. 模板裸类健康度（模板用了但 CSS 没有的 class）")
print("=" * 64)
bare = sorted(used_flat - css_classes - {'hidden', 'active', 'done'})
print(f"裸类: {len(bare)}")
for b in bare[:15]:
    # 找出在哪些模板
    where = [f.split('/')[-1] for f in glob.glob('/opt/flashshare/app/templates/**/*.html', recursive=True)
             if re.search(rf'class="[^"]*\b{re.escape(b)}\b', open(f).read())]
    print(f"   .{b}  ← {where[:3]}")

print()
print("=" * 64)
print("C. 组件家族盘点（同类异名的散乱）")
print("=" * 64)
families = {
    '行式列表': ['wd-row', 'calls-drawer .wd-row', 'usertools-item', 'bytool-list'],
    '空态': ['profile-note', 'acct-empty', 'tool_search_none'],
    '状态胶囊': ['wd-st', 'ledger-kind', 'sl-badge'],
    '卡片': ['acct-card', 'menu-sec'],
    '分页': ['acct-pager'],
}
for fam, members in families.items():
    present = [m for m in members if m.split(' ')[-1].lstrip('.') in css_classes]
    print(f"   {fam}: {present}")
