"""Operator plan/package commands — orchestration migrated verbatim in
behavior from the retired /samelabs/plans + /samelabs/packages routes.

Kept exactly (canonical tests depend on each):
  - plan field parsing + fail-closed value domains (delivery 单源 enums)
  - product binding immutability (X→Y forbidden)
  - pending-checkout price freeze
  - Creem price mirror with proven provider-before facts
  - DB commit-outcome reconciliation (exact-snapshot three-way verdict)
  - audit BEFORE any provider side effect, same transaction
  - tier validation + plan_catalog refresh
  - package create/update invariants
No payment/membership mechanism changes.
"""

import json
import logging
import re

import psycopg2

from database import db
from repo import operator_audit, orders as _orders_repo
from utils import now
from governance.delivery import EXPIRY_DAYS, DOWNLOAD_LIMITS


class PlanMirrorError(Exception):
    """Operator plan write aborted at the provider-mirror stage.

    Raised INSIDE the plan create/update transaction so the with-block
    rolls the uncommitted local row back — the provider may or may not
    have been reached, but the local plan never half-commits. Maps to
    a 502 with a stable error token."""


def _proven_provider_price(prod: dict):
    """The provider-before price must be an EXPLICIT canonical integer
    fact (Creem contract: integer cents). Anything that would need an
    int() COERCION is not a proven fact and fails closed — including
    floats (150.7 would silently truncate to 150), strings, and bool
    (Python bool is an int subclass but not a price). An explicit
    integer 0 is a legitimate fact."""
    v = prod["price"]
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _snap_equal(a: dict, b: dict) -> bool:
    """Exact equality over the plan allowlist (both sides must carry
    every key)."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    keys = ("id", "code", "name", "monthly_credits", "period_days",
            "period", "price_cents", "currency", "creem_product_id",
            "sort_order", "active", "ingress_files_24h",
            "ingress_bytes_24h", "expiry_days", "download_limits")
    for k in keys:
        if k not in a or k not in b:
            return False
        if a[k] != b[k]:
            return False
    return True


def snap_plan(r) -> dict:
    if r is None:
        return {}
    return {k: r[k] for k in (
        "id", "code", "name", "monthly_credits", "period_days", "period",
        "price_cents", "currency", "creem_product_id", "sort_order",
        "active", "ingress_files_24h", "ingress_bytes_24h",
        "expiry_days", "download_limits") if k in r.keys()}


# canonical-contract alias (mirror suites address the projection as
# _snap_plan — kept identical object, no second implementation)
_snap_plan = snap_plan


def snap_package(r) -> dict:
    if r is None:
        return {}
    return {k: r[k] for k in ("id", "code", "name", "credits",
                              "price_cents", "currency", "sort_order",
                              "active") if k in r.keys()}


def _classify_db_commit_outcome(plan_id, intended, before, actor_id,
                                tx_started, action):
    """DB-commit-outcome closure for the plan commercial mirror — a
    strict three-way verdict against EXACT full-snapshot proofs.

    The provider provably carries the intended price, but the local
    COMMIT raised / its outcome is unknown. An INDEPENDENT fresh
    connection reads the exact plan row and THIS operation's audit fact.
    Correlation is the EXACT before_json → after_json snapshot PAIR of
    this operation (both written inside the transaction from the locked
    row / RETURNING row); actor/action/target and the frozen
    tx_started boundary are additional constraints, never substitutes.

      update:  row == exact intended full snapshot + exact-pair audit
                  → DURABLE_SUCCESS
               row == exact before full snapshot + no exact-pair audit
                  → PROVEN_ROLLBACK
               any other row/audit combination → UNRESOLVED
      create:  row == exact intended snapshot + exact audit → SUCCESS
               row absent + no exact-pair audit → PROVEN_ROLLBACK
               any other → UNRESOLVED
    Never a second speculative mutation inside this classification.
    """
    from database import db as _fdb
    from repo.orders import plan_by_id as _plan_by_id
    from repo.operator_audit import recent_pair_rows as _recent_pair_rows
    try:
        with _fdb() as con:
            row = _plan_by_id(con, plan_id)
            audits = _recent_pair_rows(
                con, action=action, target_type="plan",
                target_id=str(plan_id), actor_user_id=int(actor_id),
                created_at_gte=int(tx_started), limit=20)
    except Exception:
        return "unresolved", None
    _match = False
    for a in (audits or []):
        try:
            ab = a["before_json"] if isinstance(a["before_json"], dict) \
                else json.loads(a["before_json"] or "null")
            aa = a["after_json"] if isinstance(a["after_json"], dict) \
                else json.loads(a["after_json"] or "null")
        except Exception:
            continue
        _b_ok = ((before is None and not ab) or
                 (before is not None and isinstance(ab, dict)
                  and _snap_equal(ab, before)))
        _a_ok = isinstance(aa, dict) and _snap_equal(aa, intended)
        if _b_ok and _a_ok:
            _match = True
            break
    if row is None:
        return ("rollback", None) if not _match else ("unresolved", None)
    _snap_row = snap_plan(row)
    if _snap_equal(_snap_row, intended):
        return ("success", dict(row)) if _match else ("unresolved", None)
    if before is None:
        return "unresolved", None   # update must pass its exact before
    if _snap_equal(_snap_row, before) and not _match:
        return ("rollback", dict(row))
    return "unresolved", None


_PLAN_INT_FIELDS = ("monthly_credits", "period_days", "price_cents",
                    "sort_order", "ingress_files_24h", "ingress_bytes_24h")


def parse_plan_fields(raw: dict, *, creating: bool = False) -> dict | None:
    """Operator plan field parsing + fail-closed value domains.
    Returns None = invalid request. Entitlement enums must be ⊆ the
    platform protocol (delivery single source); out-of-range → 422,
    never clipped."""
    fields: dict = {}
    if creating:
        code = str(raw.get("code", "")).strip().lower()[:32]
        if not code or not re.fullmatch(r"[a-z][a-z0-9_]*", code) or code == "free":
            return None
        fields["code"] = code
        for k in ("name",):
            v = str(raw.get(k, "")).strip()[:64]
            if not v:
                return None
            fields[k] = v
        for k in _PLAN_INT_FIELDS:
            default = 30 if k == "period_days" else 0
            v = raw.get(k, default)
            if isinstance(v, bool) or not isinstance(v, int) \
                    or v < 0 or (k == "period_days" and v <= 0):
                return None
            fields[k] = v
    else:
        if "name" in raw:
            v = str(raw["name"]).strip()[:64]
            if not v:
                return None
            fields["name"] = v
        for k in _PLAN_INT_FIELDS:
            if k in raw:
                v = raw[k]
                if isinstance(v, bool) or not isinstance(v, int) \
                        or v < 0 or (k == "period_days" and v <= 0):
                    return None
                fields[k] = v
    if "creem_product_id" in raw or creating:
        fields["creem_product_id"] = str(raw.get("creem_product_id", "")).strip()[:64] or None
    if "active" in raw or creating:
        if not isinstance(raw.get("active", True), bool):
            return None
        fields["active"] = bool(raw.get("active", True))
    for k, allowed in (("expiry_days", EXPIRY_DAYS),
                       ("download_limits", DOWNLOAD_LIMITS)):
        if k in raw or (creating and raw.get(k) is not None):
            v = raw.get(k)
            if not isinstance(v, list) or not all(
                    isinstance(x, int) and not isinstance(x, bool)
                    for x in v):
                return None
            if any(x not in allowed for x in v):
                return None  # fail-closed, never silently clipped
            fields[k] = v
    return fields


def _integrity_error_token(exc) -> str:
    msg = str(getattr(exc, "diag", None) and exc.diag.message_primary or exc)
    if "plans_paid_sort_order_uk" in msg or "sort_order" in msg:
        return "sort_order_taken"
    if "plans_product_binding_uk" in msg or "creem_product_id" in msg:
        return "product_already_bound"
    return "code_taken"


def create_plan(operator_id: int, fields: dict) -> dict:
    """Create a paid tier row (plans row = product fact source).

    Operator control-plane narrow transaction: open tx → INSERT
    uncommitted → DB unique constraints pass FIRST → (if product-bound)
    mirror Creem → provider success → COMMIT. Local constraint failure →
    ZERO provider mutation. Provider failure → DB rollback.
    Returns {'ok': True, 'plan': row} or raises PlanMirrorError /
    ConflictError(token)."""
    from repo.orders import insert_plan

    class ConflictError(Exception):
        def __init__(self, token):
            self.token = token

    row = None
    _mirror_before = None
    try:
        with db() as con:
            row = insert_plan(
                con, fields["code"], fields["name"], fields["monthly_credits"],
                fields["price_cents"], now(),
                period_days=fields["period_days"],
                sort_order=fields["sort_order"],
                creem_product_id=fields["creem_product_id"],
                ingress_files_24h=fields["ingress_files_24h"],
                ingress_bytes_24h=fields["ingress_bytes_24h"],
                expiry_days=fields.get("expiry_days") or (),
                download_limits=fields.get("download_limits") or ())
            # audit lands INSIDE the tx, BEFORE any Creem HTTP side
            # effect: audit failure → rollback → ZERO provider call;
            # Creem failure → rollback → zero committed audit row.
            operator_audit.record(con, operator_id, "plan.create", "plan",
                                  str(row["id"]), None, snap_plan(row), now())
            if fields.get("creem_product_id"):
                from economy import creem as _creem
                if not _creem.creem_enabled():
                    raise PlanMirrorError("price_mirror_unavailable")
                try:
                    _pb = _creem._fetch_product(fields["creem_product_id"])
                except Exception:
                    _pb = None
                if not isinstance(_pb, dict) \
                        or not str(_pb.get("currency") or "").strip() \
                        or "price" not in _pb or _pb["price"] is None:
                    raise PlanMirrorError("price_mirror_unavailable")
                _pb_price = _proven_provider_price(_pb)
                if _pb_price is None:
                    raise PlanMirrorError("price_mirror_unavailable")
                _mirror_before = (_pb_price,
                                  str(_pb.get("currency") or "").upper())
                _mout = _creem.mirror_price_with_reconciliation(
                    fields["creem_product_id"], _mirror_before[0],
                    _mirror_before[1],
                    int(fields["price_cents"]),
                    str(fields.get("currency") or "USD"))
                if _mout != _creem.MIRROR_SUCCESS:
                    raise PlanMirrorError("price_mirror_update_failed")
    except PlanMirrorError:
        raise
    except psycopg2.IntegrityError as exc:
        raise ConflictError(_integrity_error_token(exc))
    except psycopg2.Error:
        # COMMIT failed / outcome unknown AFTER the provider provably
        # carries the intended facts — classify from a fresh connection
        if _mirror_before and fields.get("creem_product_id"):
            from economy import creem as _ccreem
            _prod = fields["creem_product_id"]
            _bprice, _bcur = _mirror_before
            try:
                outcome, _row2 = _classify_db_commit_outcome(
                    int(row["id"]), snap_plan(row),
                    None, int(operator_id), int(row["created_at"]),
                    "plan.create")
            except Exception:
                outcome, _row2 = "unresolved", None
            if outcome == "rollback":
                if not _ccreem.restore_product_price(_prod, _bprice, _bcur):
                    raise PlanMirrorError("price_mirror_commit_unresolved")
                raise PlanMirrorError("price_mirror_local_rollback")
            if outcome == "success":
                from governance.plan_catalog import validate_config
                validate_config()
                return {"ok": True, "plan": dict(row)}
            raise PlanMirrorError("price_mirror_commit_unresolved")
        raise
    # clean exit ≠ proven COMMIT (connection drop at commit) — classify
    if _mirror_before is not None and fields.get("creem_product_id"):
        from economy import creem as _ccreem
        _prod = fields["creem_product_id"]
        _bprice, _bcur = _mirror_before
        try:
            outcome, _row2 = _classify_db_commit_outcome(
                int(row["id"]), snap_plan(row),
                None, int(operator_id), int(row["created_at"]),
                "plan.create")
        except Exception:
            outcome, _row2 = "unresolved", None
        if outcome == "rollback":
            if not _ccreem.restore_product_price(_prod, _bprice, _bcur):
                logging.getLogger("flashshare").critical(
                    "plan create mirror UNRESOLVED: product=%s before=%s/%s"
                    " intended=%s restore-unprovable",
                    _prod, _bprice, _bcur, fields["price_cents"])
                raise PlanMirrorError("price_mirror_commit_unresolved")
        elif outcome == "unresolved":
            logging.getLogger("flashshare").critical(
                "plan create mirror UNRESOLVED: product=%s before=%s/%s"
                " intended=%s db-outcome-unprovable",
                _prod, _bprice, _bcur, fields["price_cents"])
            raise PlanMirrorError("price_mirror_commit_unresolved")
    from governance.plan_catalog import validate_config
    validate_config()  # refresh + fail-closed validation
    return {"ok": True, "plan": dict(row)}


def update_plan(operator_id: int, plan_id: int, fields: dict) -> dict:
    """Configure a paid tier row: name/credits/period/price/product/
    entitlements/listing/sort (code immutable; the free row is a base
    tier, not a product). Commercial mirror write-through closes at this
    operator write, inside ONE transaction."""
    from repo.orders import update_plan as _update_plan_row
    from repo.orders import plan_by_id, plan_by_id_for_update
    from governance.plan_catalog import is_paid_tier

    class ConflictError(Exception):
        def __init__(self, token):
            self.token = token

    with db() as con:
        row = plan_by_id(con, plan_id)
    if row is None:
        raise KeyError("not_found")
    if not is_paid_tier(row["code"]):
        raise ValueError("not_editable_tier")
    commercial_change = ("price_cents" in fields) or ("creem_product_id" in fields)
    _tx_ts = now()   # THIS operation's correlation boundary (frozen)
    _db_before = None
    _mirror_before = None
    try:
        with db() as con:
            locked = plan_by_id_for_update(con, plan_id)
            before = snap_plan(locked)
            _db_before = snap_plan(locked)   # EXACT full before snapshot
            # PRODUCT BINDING IMMUTABILITY (stable provider identity)
            if "creem_product_id" in fields:
                _cur_p = locked["creem_product_id"] or None
                _new_p = fields.get("creem_product_id") or None
                if _cur_p is not None and _new_p != _cur_p:
                    raise ValueError("product_binding_immutable")
            # PENDING-CHECKOUT PRICE FREEZE
            if "price_cents" in fields:
                if _orders_repo.has_pending_plan_order(con, plan_id):
                    raise ValueError("plan_checkout_pending")
            row = _update_plan_row(con, plan_id, fields, now())
            # audit BEFORE any Creem side effect, same transaction
            operator_audit.record(con, operator_id, "plan.update", "plan",
                                  str(plan_id), before, snap_plan(row), now())
            if commercial_change:
                target_price = int(row["price_cents"])
                target_product = row["creem_product_id"] or ""
                target_currency = row["currency"] or "USD"
                if target_product:
                    from economy import creem as _creem
                    if not _creem.creem_enabled():
                        raise PlanMirrorError("price_mirror_unavailable")
                    try:
                        _pb = _creem._fetch_product(target_product)
                    except Exception:
                        _pb = None
                    if not isinstance(_pb, dict) \
                            or not str(_pb.get("currency") or "").strip() \
                            or "price" not in _pb or _pb["price"] is None:
                        raise PlanMirrorError("price_mirror_unavailable")
                    _before_price = _proven_provider_price(_pb)
                    if _before_price is None:
                        raise PlanMirrorError("price_mirror_unavailable")
                    _before_cur = str(_pb.get("currency") or "").upper()
                    _mirror_before = (_before_price, _before_cur)
                    try:
                        _mout = _creem.mirror_price_with_reconciliation(
                            target_product, _before_price, _before_cur,
                            target_price, str(target_currency))
                    except RuntimeError as exc:
                        err = "price_mirror_update_failed"
                        if "billing_contract" in str(exc) or "not_found" in str(exc):
                            err = "product_billing_contract_invalid"
                        raise PlanMirrorError(err)
                    if _mout == _creem.MIRROR_ROLLBACK:
                        raise PlanMirrorError("price_mirror_update_failed")
                    if _mout == _creem.MIRROR_UNRESOLVED:
                        raise PlanMirrorError("price_mirror_commit_unresolved")
    except (PlanMirrorError, ValueError):
        raise
    except psycopg2.IntegrityError as exc:
        raise ConflictError(_integrity_error_token(exc))
    except psycopg2.Error:
        if _mirror_before and commercial_change and row["creem_product_id"]:
            from economy import creem as _ccreem
            _prod = row["creem_product_id"]
            _bprice, _bcur = _mirror_before
            try:
                outcome, _row2 = _classify_db_commit_outcome(
                    int(plan_id), snap_plan(row),
                    _db_before, int(operator_id), int(_tx_ts),
                    "plan.update")
            except Exception:
                outcome, _row2 = "unresolved", None
            if outcome == "rollback":
                if not _ccreem.restore_product_price(_prod, _bprice, _bcur):
                    raise PlanMirrorError("price_mirror_commit_unresolved")
                raise PlanMirrorError("price_mirror_local_rollback")
            if outcome == "success":
                from governance.plan_catalog import validate_config
                validate_config()
                return {"ok": True, "plan": dict(row)}
            raise PlanMirrorError("price_mirror_commit_unresolved")
        raise
    # clean-exit commit-outcome closure
    if commercial_change and row["creem_product_id"] \
            and _mirror_before is not None:
        from economy import creem as _ccreem3
        _prod3 = row["creem_product_id"]
        _bp3, _bc3 = _mirror_before
        try:
            outcome3, _row3 = _classify_db_commit_outcome(
                int(plan_id), snap_plan(row),
                _db_before, int(operator_id), int(_tx_ts),
                "plan.update")
        except Exception:
            outcome3, _row3 = "unresolved", None
        if outcome3 == "rollback":
            if not _ccreem3.restore_product_price(_prod3, _bp3, _bc3):
                logging.getLogger("flashshare").critical(
                    "plan update mirror UNRESOLVED: product=%s before=%s/%s"
                    " intended=%s restore-unprovable",
                    _prod3, _bp3, _bc3, row["price_cents"])
                raise PlanMirrorError("price_mirror_commit_unresolved")
        elif outcome3 == "unresolved":
            logging.getLogger("flashshare").critical(
                "plan update mirror UNRESOLVED: product=%s before=%s/%s"
                " intended=%s db-outcome-unprovable",
                _prod3, _bp3, _bc3, row["price_cents"])
            raise PlanMirrorError("price_mirror_commit_unresolved")
    # POST-COMMIT READ-BACK closes the residual crash window
    if commercial_change and row["creem_product_id"]:
        from economy import creem as _creem2
        try:
            _merr = _creem2.verify_product_mirror(
                row["creem_product_id"], int(row["price_cents"]),
                str(row["currency"] or "USD"))
        except Exception:
            _merr = "verify_unavailable"
        if _merr:
            raise PlanMirrorError("price_mirror_divergence:" + str(_merr))
    from governance.plan_catalog import validate_config
    validate_config()  # refresh=True + fail-closed validation
    return {"ok": True, "plan": dict(row)}


def parse_package_fields(raw: dict, *, creating: bool = False) -> dict | None:
    """Package field parsing with fail-closed domains."""
    fields: dict = {}
    if creating:
        code = str(raw.get("code", "")).strip().lower()[:32]
        name = str(raw.get("name", "")).strip()[:64]
        if not code or not name:
            return None
        if not re.fullmatch(r"[a-z][a-z0-9_]*", code):
            return None
        fields.update({"code": code, "name": name})
    else:
        if "name" in raw:
            v = str(raw["name"]).strip()[:64]
            if not v:
                return None
            fields["name"] = v
    for k in ("credits", "price_cents") if creating else ():
        v = raw.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, int) \
                or v < 0 or (k == "credits" and v <= 0):
            return None
        fields[k] = v
    if not creating:
        for k in ("credits", "price_cents"):
            if k in raw:
                v = raw[k]
                # credits value domain >0 (matches create); price_cents >= 0
                if isinstance(v, bool) or not isinstance(v, int) \
                        or v < 0 or (k == "credits" and v <= 0):
                    return None
                fields[k] = v
        if "active" in raw:
            if not isinstance(raw["active"], bool):
                return None
            fields["active"] = raw["active"]
    if creating:
        if "active" in raw and not isinstance(raw["active"], bool):
            return None
        if "active" in raw:
            fields["active"] = raw["active"]
    return fields or None


def create_package(operator_id: int, fields: dict) -> dict:
    from repo.orders import insert_package
    try:
        with db() as con:
            row = insert_package(con, fields["code"], fields["name"],
                                 fields["credits"], fields["price_cents"],
                                 now())
            operator_audit.record(con, operator_id, "package.create",
                                  "package", str(row["id"]), None,
                                  snap_package(row), now())
    except psycopg2.IntegrityError:
        raise ValueError("code_taken")
    return {"ok": True, "package": dict(row)}


def update_package(operator_id: int, package_id: int, fields: dict) -> dict:
    from repo.orders import update_package, package_by_id_for_update
    with db() as con:
        cur = package_by_id_for_update(con, package_id)
        if cur is None:
            raise KeyError("not_found")
        before = snap_package(cur)
        row = update_package(con, package_id, fields, now())
        if row is None:
            raise KeyError("not_found")
        operator_audit.record(con, operator_id, "package.update",
                              "package", str(package_id), before,
                              snap_package(row), now())
    return {"ok": True, "package": dict(row)}
