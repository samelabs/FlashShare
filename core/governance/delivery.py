"""governance/delivery — 平台产物交付策略单源 (Stage C).

合法值/默认值只有这一份。调用方一律传 canonical 整数值; 校验用
本模块的严格 validator (无字符串强转、无非法回落)。
"""

EXPIRY_DAYS = (1, 3, 7, 30)
DOWNLOAD_LIMITS = (1, 10, 50, 100, 200, 1000)
DEFAULT_EXPIRY_DAYS = 1
DEFAULT_ALLOWED_DOWNLOADS = 50

# Tool source files are temporary execution custody, not result delivery.
# Their lifetime is platform policy and must not inherit user result settings.
INTERMEDIATE_TTL_DAYS = 1
INTERMEDIATE_DOWNLOAD_LIMIT = 10


def validate_delivery(delivery: dict) -> dict:
    """canonical delivery validator — 严格类型, 拒绝字符串/浮点/缺字段。

    delivery 必须是 {"expiry_days": int∈EXPIRY_DAYS,
    "allowed_downloads": int∈DOWNLOAD_LIMITS}。返回 canonical dict。
    非法抛 ValueError("bad_delivery")。
    """
    if not isinstance(delivery, dict):
        raise ValueError("bad_delivery")
    if set(delivery.keys()) != {"expiry_days", "allowed_downloads"}:
        raise ValueError("bad_delivery")
    ed = delivery.get("expiry_days")
    dl = delivery.get("allowed_downloads")
    if type(ed) is not int or ed not in EXPIRY_DAYS:
        raise ValueError("bad_delivery")
    if type(dl) is not int or dl not in DOWNLOAD_LIMITS:
        raise ValueError("bad_delivery")
    return {"expiry_days": ed, "allowed_downloads": dl}
