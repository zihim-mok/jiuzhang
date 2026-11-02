"""洞察工具·③——现金流预演与个人版收支推断。

数字纪律（大纲 §4.1/§4.2，P-B1 方案 A 个人版口径）：
- 所有数字可从交易表复算；不编造置信区间，不确定性以「假设+口径」表达；
- 预演结果标注「模拟预测」；缺数据期间显式标注，不补造；
- 推断口径：收入=当月全部流入（direction=in）；支出=当月全部流出（direction=out）；
  结余=收入−支出；现金流=月内按旬净流入分布（旬合计=净额，服务端断言）。
"""
from __future__ import annotations

from app.core.clock import sim_today
from app.db.connection import get_conn
from app.tools.anomaly_tools import bills_anomaly
from app.tools.bill_tools import DEMO_USER
from app.tools.registry import l0_tool, result, validate_month

_PERIODS = [("上旬（1-10 日）", 1, 10), ("中旬（11-20 日）", 11, 20), ("下旬（21-月末）", 21, 31)]

METHOD_NOTE = ("口径：收入=当月全部流入；支出=当月全部流出（含转账与订阅扣费）；"
               "结余=收入−支出；现金流=按旬净流入分布。全部可由交易流水复算。"
               "此为个人版推断展示，不代表已被银行认可的财务报表。")


def _month_bounds(month: str) -> tuple[str, str]:
    y, m = int(month[:4]), int(month[5:7])
    hi = f"{y + 1}-01-01" if m == 12 else f"{y}-{m + 1:02d}-01"
    return f"{month}-01", hi


def _shift_month(month: str, k: int) -> str:
    y, m = int(month[:4]), int(month[5:7])
    m += k
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def _month_seq(start: str, end: str) -> list[str]:
    """闭区间月序列（start<=end，YYYY-MM）。"""
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur = _shift_month(cur, 1)
    return out


@l0_tool("insights.personal_statement")
def personal_statement(month: str) -> dict:
    """个人版四项推断（收入/支出/结余/现金流按旬），缺数据显式标注。"""
    validate_month(month)
    lo, hi = _month_bounds(month)
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT txn_time, direction, amount_cents FROM transactions"
            " WHERE user_id=? AND txn_time >= ? AND txn_time < ?",
            (DEMO_USER, lo, hi)).fetchall()
        first = conn.execute(
            "SELECT MIN(substr(txn_time,1,7)) AS m FROM transactions WHERE user_id=?",
            (DEMO_USER,)).fetchone()
    finally:
        conn.close()
    first_month = first["m"] if first else None

    # 覆盖状态（缺口标注纪律：无数据/未来/不完整 都显式说明，不补造）
    today = sim_today()
    sim_month = f"{today.year:04d}-{today.month:02d}"
    if first_month is None or month < first_month:
        coverage = {"status": "no_data", "note": f"该月早于数据期起点（{first_month}），无交易记录，如实展示为零，不补造。"}
    elif month > sim_month:
        coverage = {"status": "future", "note": "未来月份无交易数据，不做无依据推断。如需预演请使用现金流预演（含假设条件）。"}
    elif month == sim_month:
        coverage = {"status": "partial", "note": f"模拟当前月（{sim_month}）数据不完整（仅截至 {today.isoformat()} 前），金额偏低属正常，不参与趋势基线。"}
    else:
        coverage = {"status": "complete", "note": "完整历史月。"}

    income = sum(r["amount_cents"] for r in rows if r["direction"] == "in")
    expense = sum(r["amount_cents"] for r in rows if r["direction"] == "out")
    income_count = sum(1 for r in rows if r["direction"] == "in")
    expense_count = sum(1 for r in rows if r["direction"] == "out")
    balance = income - expense

    cashflow = []
    for label, d1, d2 in _PERIODS:
        sel = [r for r in rows if d1 <= int(r["txn_time"][8:10]) <= d2]
        pin = sum(r["amount_cents"] for r in sel if r["direction"] == "in")
        pout = sum(r["amount_cents"] for r in sel if r["direction"] == "out")
        cashflow.append({"period": label, "in_cents": pin, "out_cents": pout,
                         "net_cents": pin - pout})
    # 服务端断言：旬现金流合计 = 月结余（可复算）
    assert sum(p["net_cents"] for p in cashflow) == balance, "旬现金流合计与结余不一致"

    return result("insights.personal_statement", {"month": month}, {
        "period": month,
        "income_cents": income, "income_count": income_count,
        "expense_cents": expense, "expense_count": expense_count,
        "balance_cents": balance,
        "cashflow_by_period": cashflow,
        "coverage": coverage,
        "method_note": METHOD_NOTE,
    })


def _anomaly_txn_ids(month: str) -> set[str]:
    r = bills_anomaly(month)
    return {it["txn_id"] for it in r["data"]["items"]}


@l0_tool("insights.cashflow_forecast")
def cashflow_forecast(weekly_adjust_cents: int = 0, months_ahead: int = 3,
                      exclude_anomalies: bool = False) -> dict:
    """现金流预演：完整月均值确定性外推 + 参数化干预，假设显式、标注模拟预测。

    exclude_anomalies 默认值 2026-10-22 由 True 改为 False（审阅问题 P0-2 连带修正）：
    - 原默认「先剔除 P1-P4 命中交易再取均值」，在只有几个月数据的场景下，**单笔大额异常
      就能把月均支出拉走一大截**（seed：剔除后 2,753.19 元 vs 全口径 3,911.63 元，差 29.6%），
      而用户界面上只看到最终一个数字，不知道口径已被改动；
    - 现默认全口径（不做未声明的调整），同时把「剔除口径」的结果作为 sensitivity 一并返回，
      让差额显式暴露给用户与前端，而不是藏在默认值里。
    """
    if not isinstance(weekly_adjust_cents, int) or isinstance(weekly_adjust_cents, bool):
        raise ValueError("weekly_adjust_cents 须为整数（分，可为负）")
    if not isinstance(months_ahead, int) or isinstance(months_ahead, bool) \
            or not 1 <= months_ahead <= 12:
        raise ValueError("months_ahead 须为 1-12 整数")

    today = sim_today()
    sim_month = f"{today.year:04d}-{today.month:02d}"
    conn = get_conn()
    try:
        first = conn.execute(
            "SELECT MIN(substr(txn_time,1,7)) AS m FROM transactions WHERE user_id=?",
            (DEMO_USER,)).fetchone()
        rows = conn.execute(
            "SELECT txn_id, txn_time, direction, amount_cents FROM transactions WHERE user_id=?",
            (DEMO_USER,)).fetchall()
    finally:
        conn.close()
    first_month = first["m"] if first else None

    complete = _month_seq(first_month, _shift_month(sim_month, -1)) if first_month else []
    if not complete:
        raise ValueError("数据期尚无完整月份，无法外推；如实告知，不硬算。")

    # 逐月收支：先在「全口径」上汇总，再单独算一份「剔除异常」的口径作敏感性对照
    anomalies: dict[str, set[str]] = {}
    excluded = 0
    for m in complete:
        anomalies[m] = _anomaly_txn_ids(m)
        excluded += len(anomalies[m])

    def _sum(drop_anomaly: bool) -> tuple[int, int]:
        tin = tout = 0
        for r in rows:
            m = r["txn_time"][:7]
            if m not in complete:
                continue
            if drop_anomaly and r["txn_id"] in anomalies.get(m, set()):
                continue
            if r["direction"] == "in":
                tin += r["amount_cents"]
            else:
                tout += r["amount_cents"]
        return tin, tout

    n = len(complete)
    full_in, full_out = _sum(False)
    excl_in, excl_out = _sum(True)

    # 主口径：按调用参数决定（默认全口径）
    base_in, base_out = (excl_in, excl_out) if exclude_anomalies else (full_in, full_out)
    avg_income = base_in // n
    avg_expense = base_out // n
    avg_net = avg_income - avg_expense

    # 敏感性：把另一种口径也算出来，差额显式交给用户判断
    alt_in, alt_out = (full_in, full_out) if exclude_anomalies else (excl_in, excl_out)
    alt_expense = alt_out // n
    expense_gap = avg_expense - alt_expense
    gap_ratio = (abs(expense_gap) / max(avg_expense, 1))
    sensitivity = {
        "compare": "全口径（不剔除）" if exclude_anomalies else "剔除异常",
        "monthly_avg_expense_cents": alt_expense,
        "monthly_avg_net_cents": (alt_in // n) - alt_expense,
        "expense_diff_cents": expense_gap,
        "expense_diff_ratio": round(gap_ratio, 4),
        "warning": (f"两种口径的月均支出相差 {abs(expense_gap) / 100:,.2f} 元"
                    f"（{gap_ratio * 100:.1f}%），说明异常交易的金额对基线影响很大，"
                    f"请结合具体笔数判断采用哪种口径。")
        if gap_ratio >= 0.10 else "",
    }

    # 干预：每周调整额按 52/12 折算为月度效应（整数千分位向下取整）
    monthly_effect = (weekly_adjust_cents * 52) // 12
    adjusted_net = avg_net + monthly_effect

    forecast_months = [_shift_month(sim_month, i) for i in range(1, months_ahead + 1)]
    per_month = [{"month": m, "net_cents": adjusted_net} for m in forecast_months]

    assumptions = [
        f"基线为完整月（{complete[0]} 至 {complete[-1]}，共 {n} 个月）算术平均；"
        f"模拟当前月（{sim_month}）数据不完整，不参与基线。",
        (f"已剔除命中 P1-P4 异常规则的 {excluded} 笔交易后取均值（确定性规则，可复算）。")
        if exclude_anomalies else
        (f"全口径计算（未剔除异常交易，其中命中 P1-P4 的共 {excluded} 笔；"
         f"如需剔除口径请传 exclude_anomalies=true）。"),
        f"干预按每周 {weekly_adjust_cents / 100:,.2f} 元 × 52/12 折算为月度效应"
        f"（整数千分位向下取整 = {monthly_effect} 分）。",
        "假设历史收支节奏延续，未考虑季节性与一次性事件。",
    ]
    if sensitivity["warning"]:
        assumptions.append(sensitivity["warning"])

    return result("insights.cashflow_forecast", {
        "weekly_adjust_cents": weekly_adjust_cents, "months_ahead": months_ahead,
        "exclude_anomalies": exclude_anomalies,
    }, {
        "baseline": {"months": complete,
                     "monthly_avg_income_cents": avg_income,
                     "monthly_avg_expense_cents": avg_expense,
                     "monthly_avg_net_cents": avg_net,
                     "excluded_anomaly_count": excluded if exclude_anomalies else 0,
                     "anomaly_detected_count": excluded},
        "sensitivity": sensitivity,
        "intervention": {"weekly_adjust_cents": weekly_adjust_cents,
                         "monthly_effect_cents": monthly_effect},
        "forecast": {"months": forecast_months, "per_month": per_month,
                     "monthly_net_cents": adjusted_net},
        "assumptions": assumptions,
        "note": "模拟预测：确定性外推结果，非真实预测；不提供置信区间，不确定性以假设与口径表达。",
    })
