"""sharing/staging — 文件暂存单点 (P4)。

Web 上传 (app/routes/api.py)、MCP ticket PUT (app/routes/mcp_upload.py)、
MCP 内联 / URL 源 (app/routes/mcp.py _call_tool) 三处入口全部经由
stage_sources(): ingress receipt claim (配额) → Gokapi 暂存 → 成功
finalize + 严格三态核验 (resolve_outcome)。入口保留的职责只有三件:
身份/哈希语义 (kind + context, 各面的操作身份定义)、sniff 收容
(调用之前完成, 传入的已是 final 身份)、错误映射 (捕获后按各面
协议作答)。

引用已有 public_id 不经过本函数 — 引用是权限解析 (resolve_upload),
不暂存、不计入入站配额。
"""

import asyncio
import io
import logging

logger = logging.getLogger("flashshare")


class StagingError(Exception):
    """已知回滚: 暂存失败或 finalize 判定回滚 — 暂存对象已清理、
    精确配额已释放、receipt 已落 failed。调用面按 err_upload_failed
    类错误作答。"""


async def stage_sources(user_id: int, ip: str, client_ref: str,
                        scope: str, req_hash: str, files: list) -> list | dict:
    """一个逻辑上传的完整暂存链 (三入口唯一实现)。

    files: [(final_filename, final_ctype, body_bytes)] — 已过 sniff
    收容 (同族收容后的 final 身份)。req_hash: 本面的 canonical
    ingress 内容哈希 (ingress_request_hash 产物)。

    返回 durable canonical file-info 列表 (新暂存或 receipt 重放;
    重放形态 verbatim 透传 — 历史单文件 receipt 可能是 dict, 由调用
    面自行适配)。claim 的幂等/配额拒绝原样上抛 (IngressConflict /
    IngressInProgress / IngressQuotaDenied / IngressUnresolved);
    已知回滚抛 StagingError。

    receipt 原语 (claim/finalize/settle/resolve) 一律调用期导入 —
    测试注入按模块属性生效, 本模块不做 import 期快照。
    """
    from sharing.ingress_receipt import (
        claim_ingress_quota, finalize_ok, resolve_outcome,
        IngressUnresolved, OUTCOME_DURABLE_OK, OUTCOME_PROVEN_ROLLBACK)
    receipt, usage_ids, replay = claim_ingress_quota(
        user_id, ip, client_ref, req_hash,
        [len(body) for _, _, body in files], scope=scope)
    if replay is not None:
        return replay

    from governance.delivery import (INTERMEDIATE_TTL_DAYS,
                                     INTERMEDIATE_DOWNLOAD_LIMIT)
    from security import sign_source_token
    from storage import write as storage_write
    infos = []
    gids = []
    for filename, ctype, body in files:
        try:
            gokapi = await asyncio.to_thread(
                storage_write, io.BytesIO(body), filename, ctype,
                INTERMEDIATE_TTL_DAYS, INTERMEDIATE_DOWNLOAD_LIMIT,
                len(body))
        except Exception:
            logger.exception("staging write failed (%s)", filename)
            await _rollback(gids, receipt, usage_ids, user_id)
            raise StagingError("staging_write_failed") from None
        gid = (gokapi.get("FileInfo", {}) or {}).get("Id", "")
        if not gid:
            await _rollback(gids, receipt, usage_ids, user_id)
            raise StagingError("gokapi_missing_id")
        gids.append(gid)
        infos.append({
            "name": filename, "filename": filename,
            "contentType": ctype, "size": len(body),
            "stagingId": gid,
            "sourceToken": sign_source_token(user_id, gid),
            "expireAt": (gokapi.get("FileInfo", {}) or {}).get("ExpireAt"),
            "expiryDays": INTERMEDIATE_TTL_DAYS,
            "allowedDownloads": INTERMEDIATE_DOWNLOAD_LIMIT,
        })

    # 成功 finalize, 严格三态核验 (ACK 丢失先核验再回答):
    #   DURABLE_OK      → 提交已落地 — 返回存储结果, 零删除零释放;
    #   PROVEN_ROLLBACK → 仍 running = 成功事务未落地 — 立即清理
    #                     暂存对象 + 精确释放 + 落 failed, 抛 StagingError;
    #   UNRESOLVED      → 零清理零释放零改写, 抛 IngressUnresolved。
    from database import db
    try:
        with db() as con:
            finalize_ok(con, int(receipt["id"]), infos)
    except Exception:
        logger.exception("ingress finalize failed id=%s — reconciling",
                         receipt["id"])
        out = resolve_outcome(int(receipt["id"]))
        if out["outcome"] == OUTCOME_DURABLE_OK:
            return out["result"]
        if out["outcome"] == OUTCOME_PROVEN_ROLLBACK:
            await _rollback(gids, receipt, usage_ids, user_id)
            raise StagingError("finalize_proven_rollback")
        raise IngressUnresolved("err_ingress_unresolved") from None
    return infos


async def _rollback(gids, receipt, usage_ids, user_id):
    """已知回滚结算: 尽力删除暂存对象, 然后同一事务内精确配额释放 +
    receipt 落 failed。DB 结算本身失败则 receipt 留 running — 启动
    recovery 接管; 配额永不投机释放。"""
    from sharing.ingress_receipt import settle_failure
    from storage import delete as storage_delete
    for gid in gids:
        try:
            await asyncio.to_thread(storage_delete, gid)
        except Exception:
            logger.warning("rollback: gokapi cleanup failed for %s", gid)
    try:
        settle_failure(int(receipt["id"]), usage_ids, user_id)
    except Exception:
        logger.exception("ingress failure settlement failed id=%s "
                         "(receipt stays running for recovery)",
                         receipt["id"])
