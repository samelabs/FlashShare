"""governance/delivery — 平台产物交付策略单源 (Stage C).

合法值/默认值只有这一份。ToolInvocation 使用严格 canonical validator
(无字符串强转、无非法回落); 普通无工具分享的 multipart 表单可经
coerce_share_delivery 薄壳做 string → int (值域仍来自本模块常量,
禁止复制第二份枚举)。
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


def coerce_share_delivery(expiry_raw, downloads_raw) -> tuple:
    """share/multipart 表单薄壳: string → int, 非法/缺失回落平台默认。

    值域来自同一常量; 只服务普通无工具分享的表单形态, 不进入
    ToolInvocation (那边走 validate_delivery 严格版)。
    """
    try:
        ed = int(expiry_raw)
    except (TypeError, ValueError):
        ed = DEFAULT_EXPIRY_DAYS
    if ed not in EXPIRY_DAYS:
        ed = DEFAULT_EXPIRY_DAYS
    try:
        dl = int(downloads_raw)
    except (TypeError, ValueError):
        dl = DEFAULT_ALLOWED_DOWNLOADS
    if dl not in DOWNLOAD_LIMITS:
        dl = DEFAULT_ALLOWED_DOWNLOADS
    return ed, dl
