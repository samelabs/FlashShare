"""Plan catalog — the Tier read model (single source: plans rows).

A Tier IS a plans row. Everything a tier is — identity, order,
commerce, monthly credits, entitlement benefits — lives in the row;
this module is the only constructor of Tier objects in the codebase
and the only sanctioned way to compare or look up tiers.

Mechanism:
  - direction(a, b)  a.order - b.order; inputs MUST be Tier objects.
    Codes entering comparisons always come from plans-row lookups
    (by_product / by_id), so unknown codes are structurally
    unreachable here.
  - sellable  = active AND price_cents > 0 AND product bound
               AND benefits configured (expiry + download arrays).
  - startup validation: every sellable row's benefits must be a
    subset of the platform protocol values (fail-closed
    ConfigurationError, never silent pruning).

No tier literals exist outside this module's migration seed.
"""

import threading

from database import db
from repo import orders as orders_repo
from governance.delivery import EXPIRY_DAYS, DOWNLOAD_LIMITS


class ConfigurationError(RuntimeError):
    """Tier row declares values unsupported by the platform protocol."""


class Benefits(tuple):
    """Entitlement value object — shape is frozen, values come from rows."""

    __slots__ = ()

    def __new__(cls, files_24h, bytes_24h, expiry_days, download_limits):
        return super().__new__(cls, (
            int(files_24h), int(bytes_24h),
            tuple(int(x) for x in expiry_days),
            tuple(int(x) for x in download_limits),
        ))

    @property
    def ingress_files_24h(self): return self[0]

    @property
    def ingress_bytes_24h(self): return self[1]

    @property
    def expiry_days(self): return self[2]

    @property
    def download_limits(self): return self[3]

    def as_dict(self, plan_code: str | None = None):
        out = {
            "ingress_files_24h": self[0],
            "ingress_bytes_24h": self[1],
            "expiry_days": self[2],
            "download_limits": self[3],
        }
        if plan_code is not None:
            out["plan_code"] = plan_code
        return out


class Tier:
    """One plans row as the canonical tier object."""

    __slots__ = ("id", "code", "name", "badge_key", "order", "active",
                 "price_cents", "currency", "product_id",
                 "monthly_credits", "period_days", "benefits")

    def __init__(self, row: dict):
        self.id = int(row["id"])
        self.code = str(row["code"])
        self.name = str(row["name"])
        self.badge_key = row["badge_key"] or ""
        self.order = int(row["sort_order"])
        self.active = bool(row["active"])
        self.price_cents = int(row["price_cents"] or 0)
        self.currency = str(row["currency"] or "")
        self.product_id = row["creem_product_id"] or ""
        self.monthly_credits = int(row["monthly_credits"] or 0)
        # DB 行是事实: period_days 由 DB CHECK 保证 >0, read-model 不再
        # 把 0 fallback 成 30 (writer/read-model 漂移已收口)
        self.period_days = int(row["period_days"])
        self.benefits = Benefits(
            row["ingress_files_24h"], row["ingress_bytes_24h"],
            row["expiry_days"] or (), row["download_limits"] or ())

    @property
    def is_free(self) -> bool:
        return self.code == "free"

    @property
    def sellable(self) -> bool:
        """单一可售 authority: active 非 free + 定价 + 绑品 + 权益配齐。
        所有消费面 (页面/subscribe/upgrade/downgrade) 只读本判定,
        禁止自行重复实现 readiness。"""
        return (self.active and not self.is_free
                and self.price_cents > 0 and bool(self.product_id)
                and self.benefits_configured)

    @property
    def benefits_configured(self) -> bool:
        return bool(self.benefits.expiry_days) and bool(self.benefits.download_limits)


def direction(a: "Tier", b: "Tier") -> int:
    """Positive = a is the higher tier. Inputs must be Tier objects."""
    return a.order - b.order


_lock = threading.Lock()
_cache: list | None = None


def _rows() -> list:
    with db() as con:
        return [dict(r) for r in orders_repo.all_plans(con)]


def catalog(refresh: bool = False) -> list:
    """All tier rows ordered by sort_order. Cached per process; call
    refresh=True only from operator write paths (samelabs PATCH)."""
    global _cache
    if _cache is None or refresh:
        rows = sorted(_rows(), key=lambda r: (r["sort_order"], r["id"]))
        with _lock:
            _cache = [Tier(r) for r in rows]
    return _cache


def by_code(code: str) -> Tier | None:
    for t in catalog():
        if t.code == code:
            return t
    return None


def by_product(product_id: str) -> Tier | None:
    if not product_id:
        return None
    for t in catalog():
        if t.product_id == product_id:
            return t
    return None


def by_id(plan_id: int) -> Tier | None:
    for t in catalog():
        if t.id == int(plan_id):
            return t
    return None


def free_tier() -> Tier:
    t = by_code("free")
    if t is None:
        raise ConfigurationError("plans row 'free' is missing")
    return t


def is_paid_tier(code: str | None) -> bool:
    t = by_code(code) if code else None
    return t is not None and not t.is_free


def _validate_against_protocol(t: Tier) -> None:
    bad_ed = [d for d in t.benefits.expiry_days if d not in EXPIRY_DAYS]
    bad_dl = [d for d in t.benefits.download_limits if d not in DOWNLOAD_LIMITS]
    if bad_ed or bad_dl:
        raise ConfigurationError(
            f"tier '{t.code}' declares values unsupported by the "
            f"platform protocol: expiry={bad_ed} downloads={bad_dl}")


def validate_config() -> None:
    """Startup / operator-write validation (fail-closed, never pruned):
    - free row 必须存在 (基础档缺席 = 配置崩坏, 启动即败)
    - 每一行已声明的 expiry/download 值必须 ⊆ 平台协议 (空数组 = 未配置,
      合法; 一旦有值, 越界即败) — inactive paid tier 仍可能是 current
      Membership, protocol-value safety 覆盖全部行, 不止商品面。
    """
    for t in catalog(refresh=True):  # 先刷新: 后续判定全部基于当前 DB 行
        pass
    free_tier()  # 缺 free row → ConfigurationError
    for t in catalog():
        _validate_against_protocol(t)
