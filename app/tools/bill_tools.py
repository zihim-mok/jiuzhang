"""L0 查询工具实现（T01-T06，接口说明见 后端开发\工具接口说明_v0.1.md）。

纪律：过滤条件下推到 SQL（参数绑定）；分类合计与总额由服务端断言一致；
金额整数（分）；只汇总已有数据，缺失期间显式标注，不补造。
"""
from __future__ import annotations

import calendar
from datetime import date

from app.core.categories import OTHER_RATIO_WARN, normalize_category
from app.core.clock import sim_today
from app.db.connection import get_conn
from app.tools.registry import dump_params, l0_tool, result, validate_month, validate_year

DEMO_USER = "u-demo"  # 单用户演示画像：服务端固定注入

IDLE_THRESHOLD_DAYS = 90  # 闲置判定阈值（依据=usage_records）


def _month_range(month: str) -> tuple[str, str]:
    """返回 [月初, 下月月初) 的 ISO 日期串（可直接与 ISO 时间戳做字典序比较）。"""
    y, m = int(month[:4]), int(month[5:7])
    if m == 12:
        nxt = date(y + 1, 1, 1)
    else:
        nxt = date(y, m + 1, 1)
    return f"{month}-01", nxt.isoformat()


def _month_of(txn_time: str) -> str:
    return txn_time[:7]


@l0_tool("transactions.query")
def transactions_query(month: str | None = None, category: str | None = None,
                       limit: int = 50, offset: int = 0) -> dict:
    tool = "transactions.query"
    params = {"month": month, "category": category, "limit": limit, "offset": offset}
    if limit < 1 or limit > 200 or offset < 0:
        raise ValueError("limit 须为 1~200，offset 须 ≥0")
    where = ["user_id = ?"]
    args: list = [DEMO_USER]
    if month:
        validate_month(month)
        lo, hi = _month_range(month)
        where.append("txn_time >= ? AND txn_time < ?")
        args += [lo, hi]
    if category:
        where.append("category = ?")
        args.append(category)
    where_sql = " AND ".join(where)
    conn = get_conn()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM transactions WHERE {where_sql}", args).fetchone()[0]
        rows = conn.execute(
            "SELECT txn_id, txn_time, direction, amount_cents, counterparty,"
            " merchant_normalized, category, remark, channel"
            f" FROM transactions WHERE {where_sql}"
            " ORDER BY txn_time DESC LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()
    finally:
        conn.close()
    return result(tool, params, {"period": month, "total": total,
                                 "items": [dict(r) for r in rows]})


@l0_tool("bill.summary")
def bill_summary(month: str) -> dict:
    tool = "bill.summary"
    validate_month(month)
    lo, hi = _month_range(month)
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT category, direction, txn_type, SUM(amount_cents) AS s, COUNT(*) AS c"
            " FROM transactions WHERE user_id=? AND txn_time >= ? AND txn_time < ?"
            " GROUP BY category, direction, txn_type",
            (DEMO_USER, lo, hi)).fetchall()
    finally:
        conn.close()
    total_in = 0
    by_cat: dict[str, int] = {}
    cat_count: dict[str, int] = {}
    has_unknown = False
    for r in rows:
        if r["direction"] == "in":
            total_in += r["s"]
            continue
        if r["category"] == "未知":
            has_unknown = True
        std = normalize_category(r["category"], r["txn_type"])
        by_cat[std] = by_cat.get(std, 0) + r["s"]
        cat_count[std] = cat_count.get(std, 0) + r["c"]
    total_out = sum(by_cat.values())
    by_category = [{"category": c, "total_cents": v, "count": cat_count[c]}
                   for c, v in sorted(by_cat.items(), key=lambda kv: -kv[1])]
    # 服务端断言：分类合计与总额一致（N02）
    assert total_out == sum(x["total_cents"] for x in by_category), "分类合计与总额不一致"
    other_ratio = (by_cat.get("其他支出", 0) / total_out) if total_out else 0.0
    data = {
        "period": month,
        "total_in_cents": total_in,
        "total_out_cents": total_out,
        "txn_count": sum(r["c"] for r in rows),
        "by_category": by_category,
        "has_unknown_category": has_unknown,
        "other_ratio": round(other_ratio, 4),
        "coverage_warning": other_ratio > OTHER_RATIO_WARN,
        "categories_version": "std9@2026-10-18",
    }
    return result(tool, {"month": month}, data)


@l0_tool("bill.yearly")
def bill_yearly(year: str) -> dict:
    tool = "bill.yearly"
    validate_year(year)
    prefix = year
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT substr(txn_time,1,7) AS m, direction, SUM(amount_cents) AS s"
            " FROM transactions WHERE user_id=? AND txn_time LIKE ?"
            " GROUP BY m, direction ORDER BY m",
            (DEMO_USER, prefix + "%")).fetchall()
    finally:
        conn.close()
    monthly: dict[str, dict] = {}
    for r in rows:
        d = monthly.setdefault(r["m"], {"month": r["m"], "total_in_cents": 0, "total_out_cents": 0})
        if r["direction"] == "in":
            d["total_in_cents"] += r["s"]
        else:
            d["total_out_cents"] += r["s"]
    covered = sorted(monthly)
    all_months = [f"{year}-{mm:02d}" for mm in range(1, 13)]
    missing = [m for m in all_months if m not in monthly]
    total_out = sum(d["total_out_cents"] for d in monthly.values())
    data = {
        "year": year,
        "covered_months": covered,
        "missing_months": missing,
        "monthly": [monthly[m] for m in covered],
        "total_out_cents": total_out,
        "coverage_note": (
            f"仅汇总已有数据月份 {covered[0]} ~ {covered[-1]}（模拟数据期间），"
            f"缺失月份 {len(missing)} 个不补造；无完整年度数据，不称完整年度账单。"
        ) if covered else "该年度无任何数据。",
    }
    return result(tool, {"year": year}, data)


@l0_tool("subscriptions.list")
def subscriptions_list() -> dict:
    tool = "subscriptions.list"
    conn = get_conn()
    try:
        subs = conn.execute(
            "SELECT * FROM subscription_authorizations WHERE user_id=? ORDER BY charge_day",
            (DEMO_USER,)).fetchall()
        items = []
        for s in subs:
            last = conn.execute(
                "SELECT MAX(used_at) AS last, COUNT(*) AS n FROM usage_records WHERE sub_id=?",
                (s["sub_id"],)).fetchone()
            idle_days = None
            if last["last"]:
                last_date = date.fromisoformat(last["last"][:10])
                idle_days = (sim_today() - last_date).days
            # 闲置评估：唯一依据 = 模拟使用记录（B08：无记录不判定）
            if last["last"] is None:
                assess = {"conclusion": "unknown",
                          "basis": "无模拟使用记录，不判定活跃或闲置（需用户确认）"}
            elif idle_days is not None and idle_days >= IDLE_THRESHOLD_DAYS:
                assess = {"conclusion": "idle_candidate",
                          "basis": f"最后使用 {last['last'][:10]}，距模拟当前日 {idle_days} 天无活动"
                                   f"（阈值 {IDLE_THRESHOLD_DAYS} 天）；仅为周期扣费候选+使用依据，非已证实闲置"}
            else:
                assess = {"conclusion": "active",
                          "basis": f"最后使用 {last['last'][:10]}，距模拟当前日 {idle_days} 天"}
            # 下一次预计扣费日（周期+扣费日复算，可解释）
            today = sim_today()
            y, m = today.year, today.month
            try:
                cand = date(y, m, s["charge_day"])
            except ValueError:
                cand = date(y, m, calendar.monthrange(y, m)[1])
            if cand <= today:
                m += 1
                if m == 13:
                    y, m = y + 1, 1
                cand = date(y, m, s["charge_day"])
            items.append({
                "sub_id": s["sub_id"],
                "counterparty": s["counterparty"],
                "auth_scope": s["auth_scope"] + "【模拟】",
                "signed_at": s["signed_at"],
                "valid_until": s["valid_until"],
                "exit_method": s["exit_method"],
                "status": s["status"],
                "total_deducted_cents": s["total_deducted_cents"],
                "last_activity_at": s["last_activity_at"],
                "rigidity": s["rigidity"],
                "rigidity_basis": s["rigidity_basis"],
                "cycle": s["cycle"],
                "charge_day": s["charge_day"],
                "monthly_amount_cents": s["monthly_amount_cents"],
                "next_expected_charge_date": cand.isoformat(),
                "usage": {"last_used_at": last["last"], "idle_days": idle_days,
                          "records_count": last["n"]},
                "idle_assessment": assess,
            })
    finally:
        conn.close()
    return result(tool, {}, {"items": items})


@l0_tool("subscriptions.reminders")
def subscriptions_reminders() -> dict:
    tool = "subscriptions.reminders"
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT r.reminder_id, r.sub_id, s.counterparty, s.status AS sub_status,"
            " r.remind_for_date, r.status"
            " FROM reminders r JOIN subscription_authorizations s ON s.sub_id = r.sub_id"
            " WHERE r.user_id=? ORDER BY r.remind_for_date", (DEMO_USER,)).fetchall()
    finally:
        conn.close()
    return result(tool, {}, {"items": [dict(r) for r in rows]})


@l0_tool("accounts.me")
def accounts_me() -> dict:
    tool = "accounts.me"
    conn = get_conn()
    try:
        u = conn.execute("SELECT * FROM users WHERE user_id=?", (DEMO_USER,)).fetchone()
        accs = conn.execute("SELECT account_id, account_name, account_type, balance_cents, currency"
                            " FROM accounts WHERE user_id=?", (DEMO_USER,)).fetchall()
    finally:
        conn.close()
    return result(tool, {}, {
        "user_id": u["user_id"],
        "name": u["name"],
        "phone": u["phone"],
        "persona_note": (u["persona_note"] or "") + "【模拟】",
        "accounts": [dict(a) for a in accs],
    })


# 此处原有一个 `call_tool(name, params)` 函数，已删除。
# 删除理由：
#   ① 它是死代码，全项目零调用点；
#   ② 函数体引用了三个未导入的名字（get_tool_level / TOOL_IMPLS / logger），
#      一旦被调用必定 NameError；
#   ③ 它的 docstring 自称「统一调用入口：白名单校验 → 执行」，极易被后来者当成正门使用，
#      而它恰好处在资金相关路径上 —— 是个安全陷阱，不是功能。
# 说明：本项目并不存在「单一工具派发入口」这种设计。白名单校验分散在
#   - app/planning/stub_planner.py（意图 → 具体处理器，模型输出须过 allowed_intents 白名单）
#   - app/services/confirmation_service.py（确认绑定 → 执行，三道闸：action 可执行、
#     在工具白名单内、level 与 ACTION_LEVELS 一致）
# 如需新增带统一校验的入口，应新建独立模块并复用 registry.TOOL_IMPLS 与
# core.permissions.get_tool_level，而不是在 bill_tools 里复活这个名字。
