"""Operator content commands (file/result governance).

The destructive mutation is ALWAYS the sharing single point —
sharing.uploads.delete_upload (mapping row + Gokapi physical delete,
owner-scoped) — never a second delete implementation. The operator audit
fact rides INSIDE that transaction via the audit hook, so audit and
deletion commit or roll back together.
"""

from database import db
from repo import operator_audit
from utils import now


def delete_upload(operator_id: int, upload_id: int) -> dict:
    """File takedown: remove one shared/result file. The routing read
    (owner lookup) is outside; the FOR UPDATE read, the row delete and
    the operator_audit fact share ONE transaction inside the sharing
    single point. Concurrent takedowns converge on the row lock — one
    delete, one audit. The gokapi_id storage binding never enters the
    audit."""
    from repo.uploads import by_id
    with db() as con:
        row = by_id(con, int(upload_id))
    if row is None:
        raise KeyError("not_found")

    def _audit(con, owned):
        operator_audit.record(
            con, operator_id, "upload.delete", "upload",
            str(int(upload_id)),
            {"id": int(owned["id"]), "user_id": int(owned["user_id"]),
             "public_id": owned["public_id"],
             "file_name": owned["file_name"],
             "content_type": owned["content_type"],
             "size_bytes": int(owned["size_bytes"])},
            None, now())

    from sharing.uploads import delete_upload as sharing_delete
    deleted = sharing_delete(int(upload_id), int(row["user_id"]), audit=_audit)
    return {"id": int(upload_id), "deleted": bool(deleted)}
