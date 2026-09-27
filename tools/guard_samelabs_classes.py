#!/usr/bin/env python3
"""Guard: every class used on the operator surface must be defined.

Scope: app/templates/samelabs/*.html + static/pages/samelabs_*.js.
A class is legal when one of:
  1. defined in static/css/tokens.css / components.css / samelabs.css;
  2. prefixed js- (pure JS hook, carries no style);
  3. listed in VALUE_TOKENS below — bare identifiers extracted from
     Jinja conditional EXPRESSIONS (status comparisons, dict keys),
     which the naive scanner picks out of class="...{{ ... }}..." but
     which are data values, not class names. Each entry names its
     source template.
Exit 0 = clean; exit 1 = ghost classes found (listed).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Jinja expression value tokens (NOT classes). Source in comments.
VALUE_TOKENS = {
    "suspended",   # users.html / user_detail.html — u.status == 'suspended'
    "ok", "err",   # toast kind classList — defined via .sl-toast.ok/.err
    "open",        # economy_reviews.html — r.status == 'open'
    "active",      # sl-pager/sl-sidenav a.active — defined in samelabs.css
    "miss",        # commerce_products.html — tier-fact miss modifier
    "hidden",      # tier-field-create.hidden / .hidden — defined
}

STYLESHEETS = ("static/css/tokens.css", "static/css/components.css",
               "static/samelabs.css")


def collect_used():
    used = {}
    files = sorted((ROOT / "app/templates/samelabs").glob("*.html")) + \
        sorted((ROOT / "static/pages").glob("samelabs_*.js"))
    for path in files:
        src = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(ROOT))
        for m in re.finditer(r'class="([^"]+)"', src):
            value = m.group(1)
            # {%%} blocks are control flow: their class contributions are
            # plain text OUTSIDE the block (e.g. " miss"); drop the block
            # itself so keywords never leak in as class tokens.
            value = re.sub(r"{%[^%]*%}", " ", value)
            # {{...}} interpolation: the quoted tokens inside are candidate
            # dynamic class names (checked below); the static prefix/suffix
            # tokens are checked like any other class.
            exprs = re.findall(r"{{([^}]*)}}", value)
            value = re.sub(r"{{[^}]*}}", " ", value)
            for cls in value.split():
                used.setdefault(cls, set()).add(rel)
            for expr in exprs:
                for tok in re.findall(r"'([\w-]+)'", expr):
                    used.setdefault(tok, set()).add(rel)
        for m in re.finditer(r"className\s*=\s*'([^']+)'", src):
            for cls in m.group(1).split():
                used.setdefault(cls, set()).add(rel)
        for m in re.finditer(r"classList\.(?:add|remove|toggle)\('([^']+)'",
                             src):
            used.setdefault(m.group(1), set()).add(rel)
    return used


def collect_defined():
    defined = set()
    for sheet in STYLESHEETS:
        css = (ROOT / sheet).read_text(encoding="utf-8")
        for m in re.finditer(r"\.([A-Za-z_][\w-]*)", css):
            defined.add(m.group(1))
    return defined


def main() -> int:
    used = collect_used()
    defined = collect_defined()
    ghosts = sorted(
        cls for cls in used
        if cls not in defined
        and not cls.startswith("js-")
        and cls not in VALUE_TOKENS
        and re.fullmatch(r"[A-Za-z][\w-]*", cls)
    )
    if ghosts:
        print("GHOST CLASSES on the operator surface:")
        for cls in ghosts:
            print(f"  .{cls}  used in: {', '.join(sorted(used[cls]))}")
        print("Every class must be defined in tokens/components/samelabs "
              "css, use the js- hook prefix, or be whitelisted as a value "
              "token in tools/guard_samelabs_classes.py.")
        return 1
    print(f"operator-surface class guard: clean ({len(used)} classes checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
