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

# Seed: category → icon mapping (curated). Icons validated against
# tools.VALID_ICONS at seed; a bad mapping fails loudly at startup.
SEED_CATEGORIES = [
    ("image",      {"zh": "图片处理", "en": "Image"},      "image"),
    ("document",   {"zh": "文档处理", "en": "Document"},   "doc"),
    ("pdf",        {"zh": "PDF", "en": "PDF"},             "pdf"),
    ("sheet",      {"zh": "表格处理", "en": "Spreadsheet"}, "sheet"),
    ("slides",     {"zh": "演示文稿", "en": "Slides"},     "slides"),
    ("archive",    {"zh": "压缩解压", "en": "Archive"},    "archive"),
    ("text",       {"zh": "文本代码", "en": "Text & Code"}, "text"),
    ("audio",      {"zh": "音频处理", "en": "Audio"},      "convert"),
    ("video",      {"zh": "视频处理", "en": "Video"},      "convert"),
    ("convert",    {"zh": "格式转换", "en": "Convert"},    "convert"),
    ("ai",         {"zh": "AI 增强", "en": "AI enhance"},  "wand"),
    ("util",       {"zh": "实用工具", "en": "Utility"},    "shield"),
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
