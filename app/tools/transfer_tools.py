"""F3 转账与定时任务工具（T11-T15、T17）与 AA（T16）。

纪律：
- T12/T13/T15 为 L2，仅经确认服务 approve 事件执行；参数取自确认单快照。
- 资金写入走统一入口 _execute_transfer：幂等键 + 余额复验 + 事务。
- 定时执行按 authorized_params_json 快照，不重新生成参数；失败不改变金额/对象/日期。
"""
from __future__ import annotations

import json
import uuid
from datetime import date

from app.core.clock import sim_today
from app.core.business_policy import require_write_enabled
from app.db.connection import get_conn
from app.planning.sessions import now8
from app.tools.bill_tools import DEMO_USER, validate_month
from app.tools.registry import l0_tool, l2_tool, result


class TransferError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def _get_account(conn) -> dict:
    return dict(conn.execute(
        "SELECT account_id, balance_cents FROM accounts WHERE user_id=?",
        (DEMO_USER,)).fetchone())


def _get_contact(conn, contact_id: str) -> dict:
    row = conn.execute(
        "SELECT contact_id, name, phone FROM contacts WHERE contact_id=? AND user_id=?",
        (contact_id, DEMO_USER)).fetchone()
    if row is None:
        raise TransferError("NOT_FOUND", f"收款对象 {contact_id} 不存在")
    return dict(row)


def _validate_amount(amount_cents) -> int:
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
        raise TransferError("VALIDATION_ERROR", "金额必须为正整数（分）")
    return amount_cents


def _execute_transfer(conn, contact_id: str, amount_cents: int, request_id: str,
                      remark: str = "", txn_type: str = "transfer") -> dict:
    """统一资金执行入口：幂等 → 复验 → 扣款 → 交易 → 凭证。调用方负责外层事务语义。"""
    require_write_enabled()
    amount_cents = _validate_amount(amount_cents)
    existing = conn.execute(
        "SELECT status, result_txn_id FROM idempotency_keys WHERE request_id=?",
        (request_id,)).fetchone()
    if existing and existing["status"] == "done":
        txn = conn.execute("SELECT * FROM transactions WHERE txn_id=?",
                           (existing["result_txn_id"],)).fetchone()
        return {"txn_id": existing["result_txn_id"], "idempotent_replay": True,
                "amount_cents": txn["amount_cents"] if txn else None}
    if existing and existing["status"] == "processing":
        # 上次执行中断：先核查，不盲目重扣（B13）
        raise TransferError("NEEDS_REVIEW",
                            f"请求 {request_id} 处于处理中，请先核查实际结果再操作")
    contact = _get_contact(conn, contact_id)
    account = _get_account(conn)
    if account["balance_cents"] < amount_cents:
        conn.execute(
            "INSERT OR REPLACE INTO idempotency_keys (request_id, action, status, created_at)"
            " VALUES (?, 'transfer', 'failed', ?)",
            (request_id, now8().isoformat()))
        raise TransferError("INSUFFICIENT_FUNDS",
                            f"余额不足：当前 {account['balance_cents']} 分，需 {amount_cents} 分")
    txn_id = f"tx-{uuid.uuid4().hex[:12]}"
    conn.execute(
        "UPDATE accounts SET balance_cents = balance_cents - ? WHERE account_id=?",
        (amount_cents, account["account_id"]))
    conn.execute(
        "INSERT INTO transactions (txn_id, user_id, account_id, txn_time, direction,"
        " amount_cents, counterparty, merchant_normalized, category, txn_type, remark, channel)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (txn_id, DEMO_USER, account["account_id"], now8().isoformat(), "out",
         amount_cents, contact["name"], contact["name"], "其他", txn_type, remark, "模拟渠道"))
    conn.execute(
        "INSERT OR REPLACE INTO idempotency_keys (request_id, action, status, result_txn_id, created_at)"
        " VALUES (?, 'transfer', 'done', ?, ?)",
        (request_id, txn_id, now8().isoformat()))
    return {"txn_id": txn_id, "idempotent_replay": False, "amount_cents": amount_cents,
            "receiver_name": contact["name"]}


@l0_tool("transfers.preview")
def transfers_preview(contact_id: str, amount_cents: int) -> dict:
    _validate_amount(amount_cents)
    conn = get_conn()
    try:
        contact = _get_contact(conn, contact_id)
        account = _get_account(conn)
        feasible = account["balance_cents"] >= amount_cents
    finally:
        conn.close()
    return result("transfers.preview",
                  {"contact_id": contact_id, "amount_cents": amount_cents}, {
                      "account_id": account["account_id"],
                      "receiver_name": contact["name"],
                      "amount_cents": amount_cents,
                      "balance_cents": account["balance_cents"],
                      "balance_after_cents": account["balance_cents"] - amount_cents,
                      "feasible": feasible,
                  })


@l2_tool("transfers.create")
def transfers_create(contact_id: str, amount_cents: int, request_id: str) -> dict:
    conn = get_conn()
    try:
        r = _execute_transfer(conn, contact_id, amount_cents, request_id)
        balance = conn.execute("SELECT balance_cents FROM accounts WHERE user_id=?",
                               (DEMO_USER,)).fetchone()["balance_cents"]
        receipt_id = f"RT-{uuid.uuid4().hex[:10].upper()}"
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, detail_json) VALUES (datetime('now'),?,?,?)",
            ("tool:transfers.create", "transfers.create",
             json.dumps({"receipt_id": receipt_id, "txn_id": r["txn_id"],
                         "request_id": request_id, "replay": r["idempotent_replay"]},
                        ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    return result("transfers.create",
                  {"contact_id": contact_id, "amount_cents": amount_cents}, {
                      "txn_id": r["txn_id"],
                      "receipt_id": receipt_id,
                      "amount_cents": r["amount_cents"],
                      "receiver_name": r.get("receiver_name"),
                      "balance_after_cents": balance,
                      "idempotent_replay": r["idempotent_replay"],
                  })


def _next_run_for(day_of_month: int) -> str:
    today = sim_today()
    y, m = today.year, today.month
    cand = date(y, m, day_of_month)
    if cand <= today:
        m += 1
        if m == 13:
            y, m = y + 1, 1
        cand = date(y, m, day_of_month)
    return cand.isoformat()


@l2_tool("transfers.scheduled.create")
def transfers_scheduled_create(contact_id: str, amount_cents: int, day_of_month: int) -> dict:
    _validate_amount(amount_cents)
    if not isinstance(day_of_month, int) or not 1 <= day_of_month <= 28:
        raise TransferError("VALIDATION_ERROR", "扣费日须为 1-28 的整数（异常日期规则显式限定）")
    conn = get_conn()
    try:
        _get_contact(conn, contact_id)
        account = _get_account(conn)
        if account["balance_cents"] < amount_cents:
            raise TransferError("INSUFFICIENT_FUNDS", "余额不足，不创建计划（B06）")
        task_id = f"task-{uuid.uuid4().hex[:10]}"
        next_run = _next_run_for(day_of_month)
        conn.execute(
            "INSERT INTO scheduled_transfers (task_id, user_id, account_id, contact_id,"
            " amount_cents, recurrence, day_of_month, next_run_date, status,"
            " authorized_params_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, DEMO_USER, account["account_id"], contact_id, amount_cents,
             "monthly", day_of_month, next_run, "pending",
             json.dumps({"contact_id": contact_id, "amount_cents": amount_cents,
                         "day_of_month": day_of_month}, ensure_ascii=False),
             now8().isoformat()))
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, detail_json) VALUES (datetime('now'),?,?,?)",
            ("tool:transfers.scheduled.create", "transfers.scheduled.create",
             json.dumps({"task_id": task_id, "next_run_date": next_run}, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    return result("transfers.scheduled.create",
                  {"contact_id": contact_id, "amount_cents": amount_cents,
                   "day_of_month": day_of_month}, {
                      "task_id": task_id, "status": "pending",
                      "recurrence": "monthly", "day_of_month": day_of_month,
                      "next_run_date": next_run,
                      "receipt_id": f"RP-{task_id.upper()}",
                  })


@l0_tool("transfers.scheduled.list")
def transfers_scheduled_list() -> dict:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT t.task_id, t.amount_cents, t.recurrence, t.day_of_month,"
            " t.next_run_date, t.status, t.executed_txn_id, c.name AS receiver_name"
            " FROM scheduled_transfers t JOIN contacts c ON c.contact_id=t.contact_id"
            " WHERE t.user_id=? ORDER BY t.created_at", (DEMO_USER,)).fetchall()
    finally:
        conn.close()
    return result("transfers.scheduled.list", {},
                  {"items": [dict(r) for r in rows]})


def get_task(task_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT t.*, c.name AS receiver_name FROM scheduled_transfers t"
            " JOIN contacts c ON c.contact_id=t.contact_id WHERE t.task_id=?",
            (task_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


@l2_tool("transfers.scheduled.cancel")
def transfers_scheduled_cancel(task_id: str) -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT task_id, status, next_run_date FROM scheduled_transfers"
            " WHERE task_id=? AND user_id=?", (task_id, DEMO_USER)).fetchone()
        if row is None:
            raise TransferError("NOT_FOUND", f"定时任务 {task_id} 不存在")
        if row["status"] != "pending":
            raise TransferError("VALIDATION_ERROR",
                                f"任务状态为 {row['status']}，仅未执行任务可取消")
        conn.execute("UPDATE scheduled_transfers SET status='cancelled' WHERE task_id=?",
                     (task_id,))
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, detail_json) VALUES (datetime('now'),?,?,?)",
            ("tool:transfers.scheduled.cancel", "transfers.scheduled.cancel",
             json.dumps({"task_id": task_id}, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    return result("transfers.scheduled.cancel", {"task_id": task_id}, {
        "task_id": task_id, "previous_status": row["status"],
        "new_status": "cancelled",
        "receipt_id": f"RC-C{task_id[-8:].upper()}",
        "note": "未来不再触发；已产生的交易不受影响。",
    })


def run_due_tasks(as_of: str) -> dict:
    """T17 调度执行器：按授权参数快照执行到期任务（幂等）。"""
    require_write_enabled()
    as_of_date = date.fromisoformat(as_of)
    executed, failed, skipped = [], [], []
    conn = get_conn()
    try:
        tasks = conn.execute(
            "SELECT * FROM scheduled_transfers WHERE status='pending' AND next_run_date<=?"
            " ORDER BY next_run_date", (as_of_date.isoformat(),)).fetchall()
        for t in tasks:
            request_id = f"task-{t['task_id']}-{t['next_run_date']}"
            try:
                params = json.loads(t["authorized_params_json"])
                r = _execute_transfer(conn, params["contact_id"], params["amount_cents"],
                                      request_id, remark=f"定时转账（计划 {t['task_id']}）",
                                      txn_type="scheduled_transfer")
                if r.get("idempotent_replay"):
                    conn.execute(
                        "UPDATE scheduled_transfers SET status='executed', executed_txn_id=?"
                        " WHERE task_id=?", (r["txn_id"], t["task_id"]))
                    skipped.append({"task_id": t["task_id"], "reason": "幂等重放，补记执行状态",
                                    "txn_id": r["txn_id"]})
                else:
                    conn.execute(
                        "UPDATE scheduled_transfers SET status='executed', executed_txn_id=?"
                        " WHERE task_id=?", (r["txn_id"], t["task_id"]))
                    executed.append({"task_id": t["task_id"], "txn_id": r["txn_id"],
                                     "amount_cents": r["amount_cents"]})
            except TransferError as e:
                if e.code == "NEEDS_REVIEW":
                    skipped.append({"task_id": t["task_id"], "reason": e.message})
                    continue
                # 余额不足等失败：任务转 needs_review，不改变金额/对象/日期
                conn.execute(
                    "UPDATE scheduled_transfers SET status='needs_review' WHERE task_id=?",
                    (t["task_id"],))
                failed.append({"task_id": t["task_id"], "reason": e.message})
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, detail_json) VALUES (datetime('now'),?,?,?)",
            ("tool:scheduler.run", "scheduler.run",
             json.dumps({"as_of": as_of, "executed": len(executed),
                         "failed": len(failed), "skipped": len(skipped)},
                        ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    return {"as_of": as_of, "executed": executed, "failed": failed, "skipped": skipped}
