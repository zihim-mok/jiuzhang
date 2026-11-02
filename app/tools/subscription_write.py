"""订阅写操作与洞察工具（v0.2 增补：T07/T08）。

纪律：T07 仅经确认服务 approve 事件触发；重复取消幂等跳过；
T08 只做服务端复算，「减少扣费 ≠ 已收到现金」由规划层表达。
"""
from __future__ import annotations

import json
import uuid

from app.db.connection import get_conn
from app.tools.registry import dump_params, l0_tool, l1_tool, result
from app.tools.bill_tools import DEMO_USER


@l1_tool("subscriptions.cancel")
def subscriptions_cancel(sub_ids: list[str]) -> dict:
    tool = "subscriptions.cancel"
    if not sub_ids or not isinstance(sub_ids, list):
        raise ValueError("sub_ids 必须为非空列表")
    conn = get_conn()
    try:
        cancelled, skipped = [], []
        for sid in sub_ids:
            row = conn.execute(
                "SELECT sub_id, counterparty, status, monthly_amount_cents"
                " FROM subscription_authorizations WHERE sub_id=? AND user_id=?",
                (sid, DEMO_USER)).fetchone()
            if row is None:
                raise ValueError(f"订阅 {sid} 不存在")
            if row["status"] != "active":
                skipped.append({"sub_id": sid, "reason": f"状态为 {row['status']}，无需重复取消"})
                continue
            conn.execute(
                "UPDATE subscription_authorizations SET status='cancelled' WHERE sub_id=?",
                (sid,))
            # 取消必须连**待发的续费提醒**一起关掉。
            # 原来只改授权表状态，reminders 里那条 pending 提醒原地不动，
            # 于是「我有哪些要续费的」照样报「星橙视频 2026-11-05 待提醒」——
            # 用户已经取消的订阅还在提醒他准备续费，等于取消没发生。
            # 同属本次已获 L1 确认的操作范围，不额外要一次授权，但要在凭证里如实交代。
            hushed = conn.execute(
                "UPDATE reminders SET status='dismissed'"
                " WHERE sub_id=? AND user_id=? AND status='pending'",
                (sid, DEMO_USER)).rowcount or 0
            cancelled.append({
                "sub_id": sid, "counterparty": row["counterparty"],
                "previous_status": "active", "new_status": "cancelled",
                "monthly_amount_cents": row["monthly_amount_cents"],
                "dismissed_reminders": hushed,
            })
        receipt_id = f"RC-{uuid.uuid4().hex[:10].upper()}"
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, detail_json)"
            " VALUES (datetime('now'), ?, ?, ?)",
            ("tool:subscriptions.cancel", "subscriptions.cancel",
             json.dumps({"receipt_id": receipt_id, "cancelled": cancelled,
                         "skipped": skipped}, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    return result(tool, {"sub_ids": sub_ids},
                  {"cancelled": cancelled, "skipped": skipped, "receipt_id": receipt_id})


@l0_tool("insights.savings")
def insights_savings(cancelled_sub_ids: list[str]) -> dict:
    tool = "insights.savings"
    conn = get_conn()
    try:
        saved = 0
        for sid in cancelled_sub_ids:
            row = conn.execute(
                "SELECT monthly_amount_cents FROM subscription_authorizations"
                " WHERE sub_id=? AND user_id=? AND status='cancelled'",
                (sid, DEMO_USER)).fetchone()
            if row is None:
                raise ValueError(f"订阅 {sid} 不存在或不是已取消状态，拒绝计算")
            saved += row["monthly_amount_cents"]
        remaining = conn.execute(
            "SELECT COALESCE(SUM(monthly_amount_cents),0) AS s"
            " FROM subscription_authorizations"
            " WHERE user_id=? AND rigidity='flexible' AND status='active'",
            (DEMO_USER,)).fetchone()["s"]
    finally:
        conn.close()
    return result(tool, {"cancelled_sub_ids": cancelled_sub_ids}, {
        "monthly_saved_cents": saved,
        "monthly_remaining_cents": remaining,
        "monthly_total_flexible_cents": saved + remaining,
        "basis": "monthly_saved = Σ(已取消订阅 monthly_amount_cents)，工具复算",
    })
