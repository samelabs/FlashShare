"""Operator content commands (file/result governance).

The destructive mutation is ALWAYS the sharing single point —
sharing.uploads.delete_upload (mapping row + Gokapi physical delete,
owner-scoped) — never a second delete implementation.
"""

from database import db
from repo import operator_audit
from utils import now


def delete_upload(operator_id: int, upload_id: int) -> dict:
    """File takedown: remove one shared/result file. The audit fact
    commits FIRST in its own transaction, then the sharing single point
    performs the deletion (it is idempotent-by-absence: a row that
    vanished meanwhile simply means the takedown goal already holds).
    The gokapi_id storage binding never enters the audit."""
    from repo.uploads import by_id
    with db() as con:
        row = by_id(con, int(upload_id))
        if row is None:
            raise KeyError("not_found")
        before = {"id": int(row["id"]), "user_id": int(row["user_id"]),
                  "public_id": row["public_id"],
                  "file_name": row["file_name"],
                  "content_type": row["content_type"],
                  "size_bytes": int(row["size_bytes"])}
        operator_audit.record(con, operator_id, "upload.delete", "upload",
                              str(int(upload_id)), before, None, now())
    from sharing.uploads import delete_upload as sharing_delete
    sharing_delete(int(upload_id), int(row["user_id"]))
    return {"id": int(upload_id), "deleted": True}
