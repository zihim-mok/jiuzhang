"""确认服务：L1/L2 授权的唯一入口（契约 v0.1 §二.3）。

- 确认绑定：用户 + action + 规范化参数快照；同用户同 action 建新确认后旧 pending 自动失效。
- 授权只产生于 decide() 的 approve 事件；对话文本「已确认/已授权」无效（A08）。
- 重复表态/过期 → CONFIRMATION_EXPIRED（B11/B14：不重复执行）。
- 留痕：创建与表态写 audit_log；密钥不入日志。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from app.core.business_policy import require_write_enabled
from app.db.connection import get_conn
from app.planning.sessions import now8
from app.core.permissions import get_tool_level
from app.tools.registry import TOOL_IMPLS

TTL_DEFAULT_MIN = 30

# 确认可绑定的写操作 → 权限级别（服务端唯一权威；白名单外 action 一律拒绝）
ACTION_LEVELS = {
    "subscriptions.cancel": "L1",
    "transfers.create": "L2",
    "transfers.scheduled.create": "L2",
    "transfers.scheduled.cancel": "L2",
}
EXECUTABLE_ACTIONS = set(ACTION_LEVELS)


class ConfirmationError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def _audit(conn, actor: str, action: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, detail_json) VALUES (datetime('now'),?,?,?)",
        (actor, action, json.dumps(detail, ensure_ascii=False)))


def create(user_id: str, action: str, level: str, normalized_params: dict,
           summary: str, impact: list[str], rows: list[list] | None = None,
           ttl_min: int = TTL_DEFAULT_MIN) -> dict:
    # 动作粒度写门禁：只放行业务策略已启用的动作
    require_write_enabled(action)
    if action not in EXECUTABLE_ACTIONS:
        raise ConfirmationError("VALIDATION_ERROR", f"action {action} 不可确认执行")
    if get_tool_level(action) is None:
        raise ConfirmationError("VALIDATION_ERROR", f"action {action} 不在工具白名单")
    if level != ACTION_LEVELS.get(action):
        raise ConfirmationError("VALIDATION_ERROR",
                                f"action {action} 权限级别应为 {ACTION_LEVELS.get(action)}")
    cid = f"c-{uuid.uuid4().hex[:12]}"
    created = now8()
    expires = created + timedelta(minutes=ttl_min)
    conn = get_conn()
    try:
        # 同用户同 action 的旧 pending 全部失效（参数变即失效 / 新确认替代旧确认）
        conn.execute(
            "UPDATE confirmations SET status='expired', decided_at=?"
            " WHERE user_id=? AND action=? AND status='pending'",
            (created.isoformat(), user_id, action))
        conn.execute(
            "INSERT INTO confirmations (confirmation_id, user_id, action, level,"
            " normalized_params_json, status, created_at, expires_at)"
            " VALUES (?,?,?,?,?,'pending',?,?)",
            (cid, user_id, action, level, json.dumps(normalized_params, ensure_ascii=False),
             created.isoformat(), expires.isoformat()))
        _audit(conn, "service:confirmation", "confirmation.create",
               {"confirmation_id": cid, "action": action, "level": level,
                "params": normalized_params})
        conn.commit()
    finally:
        conn.close()
    return {
        "confirmation_id": cid, "level": level, "action": action,
        "summary": summary, "normalized_params": normalized_params,
        "impact": impact, "rows": rows or [],
        "expires_at": expires.isoformat(),
    }


def expire_pending(confirmation_id: str, reason: str = "user_noop") -> bool:
    """把 pending 确认单置为 expired。

    与 decide() 的分工：decide 是用户对**某张卡**表态；这里是用户**放弃整个操作**
    （「我不想取消，先算了」）。不置失效的话，卡还留在界面上、点了照样执行 ——
    前端只禁用被点的那一张，历史卡不回收。
    返回是否真的改动了状态（供调用方决定文案，不谎称已失效）。
    不设写门禁：本函数只把 pending 置为 expired（让卡不可执行），
    是"让世界更安全"方向的变更，不产生任何业务数据写入。
    """
    conn = get_conn()
    try:
        # 原来「先读 status 再无条件 UPDATE」，
        # 与 decide() 并发时可能把一张正在执行的卡改成 expired ——
        # 钱动了、记录却写着「已失效」。改成把 status='pending' 写进 WHERE，
        # 让 rowcount 决定谁改得动（同 decide 的抢占思路）。
        cur = conn.execute(
            "UPDATE confirmations SET status='expired', decided_at=?"
            " WHERE confirmation_id=? AND status='pending'",
            (now8().isoformat(), confirmation_id))
        if cur.rowcount != 1:
            conn.rollback()
            return False
        _audit(conn, "service:confirmation", "confirmation.expire_noop",
               {"confirmation_id": confirmation_id, "reason": reason})
        conn.commit()
        return True
    finally:
        conn.close()


def decide(confirmation_id: str, decision: str) -> dict:
    if decision not in ("approve", "reject"):
        raise ConfirmationError("VALIDATION_ERROR", "decision 须为 approve 或 reject")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM confirmations WHERE confirmation_id=?",
            (confirmation_id,)).fetchone()
        if row is None:
            raise ConfirmationError("NOT_FOUND", "确认单不存在")
        if row["status"] != "pending":
            raise ConfirmationError("CONFIRMATION_EXPIRED",
                                    f"该确认已处理或已失效（状态 {row['status']}），请重新发起")
        if now8() > datetime.fromisoformat(row["expires_at"]):
            conn.execute("UPDATE confirmations SET status='expired' WHERE confirmation_id=?",
                         (confirmation_id,))
            conn.commit()
            raise ConfirmationError("CONFIRMATION_EXPIRED", "确认已过期，请重新发起")

        params = json.loads(row["normalized_params_json"])
        action = row["action"]
        # 动作粒度写门禁：卡能创建的前提是对应动作已放行，此处为第二道独立校验；
        # 模式切换后的历史卡也在这里被挡住。reject 不产生业务写入，但同样受此闸门约束——
        # 对禁用动作的卡连"拒绝"都不必走，直接以 403 语义报给前端。
        require_write_enabled(action)
        decided_at = now8().isoformat()

        # ── 原子抢占──────────────────────────
        # 原来的写法是「先 SELECT 看 pending，再执行工具，最后 UPDATE 成 approved」。
        # 中间隔着一次完整的工具执行，两个并发 approve 都会读到 pending，然后各扣一次钱。
        # 实测：两个线程对齐后同时打同一张卡，30 轮里 3 轮双扣（余额少 200 分、多一笔交易）。
        # 光有「幂等键」挡不住 —— 双线程都在幂等键写入前读到了「没有记录」。
        #
        # 现在把「pending → 最终态」这一步做成条件 UPDATE，用 rowcount 当唯一闸门：
        # SQLite 对写语句串行化，两个请求里只有一个能把 pending 改掉，另一个拿到 0 行，
        # 立刻退场。这是「设计安全」，不再靠数据库锁报错碰巧兜住。
        claimed = conn.execute(
            "UPDATE confirmations SET status=?, decided_at=?"
            " WHERE confirmation_id=? AND status='pending'",
            ("rejected" if decision == "reject" else "executing",
             decided_at, confirmation_id))
        conn.commit()
        if claimed.rowcount != 1:
            raise ConfirmationError(
                "CONFIRMATION_EXPIRED",
                "该确认正在处理中或已失效，本次未执行。请刷新后再看结果，不要重复点。")

        if decision == "reject":
            conn.execute(
                "UPDATE confirmations SET result_ref=? WHERE confirmation_id=?",
                (json.dumps({"outcome": "rejected"}), confirmation_id))
            _audit(conn, "service:confirmation", "confirmation.reject",
                   {"confirmation_id": confirmation_id})
            conn.commit()
            return {"status": "rejected",
                    "action": action,
                    "receipt": {"confirmation_id": confirmation_id,
                                "outcome": "状态未变更，操作已跳过"}}

        # approve：绑定参数执行（执行器即已注册工具；参数取自确认单快照，不取对话）
        fn = TOOL_IMPLS.get(action)
        if fn is None or get_tool_level(action) is None:
            _mark_failed(conn, confirmation_id, "VALIDATION_ERROR", "action 无可执行实现或不在白名单")
            raise ConfirmationError("VALIDATION_ERROR", "action 无可执行实现或不在白名单")
        try:
            tool_result = fn(**params)
        except Exception as e:  # noqa: BLE001 —— 任何执行期异常都不能漏成 500
            code = getattr(e, "code", None) or (
                "VALIDATION_ERROR" if isinstance(e, ValueError) else "EXECUTION_ERROR")
            message = getattr(e, "message", None) or str(e) or type(e).__name__
            _mark_failed(conn, confirmation_id, code, message)
            raise ConfirmationError(code, _EXEC_FAIL_TEXT.format(msg=message)) from e

        receipt = {"confirmation_id": confirmation_id, "action": action,
                   "receipt_id": tool_result["data"].get("receipt_id"),
                   "cancelled": tool_result["data"].get("cancelled"),
                   "skipped": tool_result["data"].get("skipped"),
                   "result": tool_result["data"]}  # 完整工具结果（含 txn_id/task_id 等）
        try:
            conn.execute(
                "UPDATE confirmations SET status='approved', result_ref=?"
                " WHERE confirmation_id=?",
                (json.dumps(receipt, ensure_ascii=False), confirmation_id))
            _audit(conn, "service:confirmation", "confirmation.approve",
                   {"confirmation_id": confirmation_id, "action": action,
                    "receipt_id": receipt.get("receipt_id")})
            conn.commit()
        except Exception as e:  # noqa: BLE001
            # 钱已经动了，只有「记账」这步失败。这里绝不能退回 rejected/pending ——
            # 那等于让用户再点一次、再扣一次。凭证已经拿到手，就以凭证为准：
            # 结果照常返回，另外挂一条 warning，确认单停在 executing（不会再被执行）。
            receipt["record_warning"] = (
                f"已执行，但确认结果回写失败（{type(e).__name__}）；"
                f"请以凭证号 {receipt.get('receipt_id') or '—'} 为准，不要重复点击。")
        return {"status": "executed", "receipt": receipt}
    finally:
        conn.close()


# 执行失败的统一口径：既要说清「没执行」，也不能让用户以为可以无脑再点一次。
_EXEC_FAIL_TEXT = "本次未执行：{msg}"


def _mark_failed(conn, confirmation_id: str, code: str, message: str) -> None:
    """把已抢占的确认单落成 failed，并记下失败原因。

    为什么不退回 pending：工具可能已经把一半写进库了（扣了款没写凭证，
    或者写了一半事务没提交），退回 pending 等于请用户再点一次 —— 那才是双扣。
    B13 的「先核查再操作」在这里落地为：卡作废，让用户看到失败原因后重新发起。
    """
    try:
        conn.execute(
            "UPDATE confirmations SET status='failed', result_ref=? WHERE confirmation_id=?",
            (json.dumps({"outcome": "failed", "error": code, "message": message},
                        ensure_ascii=False), confirmation_id))
        _audit(conn, "service:confirmation", "confirmation.failed",
               {"confirmation_id": confirmation_id, "error": code})
        conn.commit()
    except Exception:  # noqa: BLE001 —— 留痕失败不能盖住原始异常
        pass
