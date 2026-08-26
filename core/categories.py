"""Tool categories — the classification layer (2026-08-25 spec change).

Icons are now DERIVED, not chosen: a tool belongs to a category, the
category maps to an icon. Users pick a CATEGORY in the submit form (a
handful of meaningful choices, not a 40-icon wall); the icon follows.

This kills three problems at once:
  - 40-icon grid ate the modal (huge footprint, wrong information unit)
  - icon semantics drifted (UI glyphs like close/copy leaked into pickers)
  - every submission made a fresh icon decision instead of inheriting
    a curated mapping

Data shape (tool_categories rows):
  key         stable id ('image', 'document', ...)
  label_json  {lang: display}
  icon        ONE icon from tools.VALID_ICONS (curated at seed time)
  sort        display order
"""

import json

from database import db
from utils import now

# Seed: category = USER SCENARIO (the job to get done), NOT a file format.
# Tools are not limited to file processing — remote/AI endpoints do
# generation, analysis, extraction… The list is verb-first, covers
# first-party tools AND future community/AI capabilities:
SEED_CATEGORIES = [
    ("optimize",  {"zh": "优化压缩", "en": "Optimize"},     "compress"),
    ("convert",   {"zh": "格式转换", "en": "Convert"},      "convert"),
    ("merge",     {"zh": "合并拆分", "en": "Merge & Split"}, "merge"),
    ("generate",  {"zh": "生成创建", "en": "Generate"},     "wand"),
    ("edit",      {"zh": "编辑调整", "en": "Edit"},         "edit"),
    ("extract",   {"zh": "提取识别", "en": "Extract"},      "crop"),
    ("beautify",  {"zh": "美化润色", "en": "Beautify"},     "image"),
    ("protect",   {"zh": "安全保护", "en": "Protect"},      "shield"),
    ("analyze",   {"zh": "分析洞察", "en": "Analyze"},      "info"),
    ("share",     {"zh": "分享分发", "en": "Share"},        "upload"),
    ("automation",{"zh": "自动化", "en": "Automation"},     "settings"),
    ("other",     {"zh": "其他", "en": "Other"},            "info"),
]


def ensure_schema() -> dict:
    ts = now()
    with db() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_categories (
              id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
              key        TEXT NOT NULL UNIQUE,
              label_json TEXT NOT NULL,
              icon       TEXT NOT NULL,
              sort       INT NOT NULL DEFAULT 0,
              updated_at BIGINT NOT NULL
            )
            """
        )
        seeded = 0
        for i, (key, label, icon) in enumerate(SEED_CATEGORIES):
            cur = con.execute(
                """INSERT INTO tool_categories (key, label_json, icon, sort, updated_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (key) DO UPDATE SET
                     label_json = EXCLUDED.label_json,
                     icon = EXCLUDED.icon,
                     sort = EXCLUDED.sort,
                     updated_at = EXCLUDED.updated_at""",
                (key, json.dumps(label, ensure_ascii=False), icon, i, ts),
            )
            seeded += cur.rowcount
        # Retired seed keys: rows were deleted from SEED_CATEGORIES (e.g. the
        # format-based taxonomy 2026-08-25) — remove them so pickers show
        # only the live scenario taxonomy. Operator-added custom keys
        # (sort >= 100) survive.
        live = [k for k, _l, _i in SEED_CATEGORIES]
        cur.execute(
            "DELETE FROM tool_categories WHERE key != ALL(%s) AND sort < 100",
            (live,),
        )
    return {"seeded": seeded}


def list_categories() -> list:
    """Ordered category list for pickers (label per lang resolved client-side)."""
    with db() as con:
        rows = con.execute(
            "SELECT key, label_json, icon FROM tool_categories ORDER BY sort, key"
        ).fetchall()
    return [{"key": r["key"], "label": json.loads(r["label_json"]), "icon": r["icon"]}
            for r in rows]


def category_icon(key: str) -> str | None:
    with db() as con:
        row = con.execute(
            "SELECT icon FROM tool_categories WHERE key = %s", (key,)
        ).fetchone()
    return row["icon"] if row else None


def valid_keys() -> set:
    with db() as con:
        rows = con.execute("SELECT key FROM tool_categories").fetchall()
    return {r["key"] for r in rows}
