"""F1 异常交易识别引擎——可配置确定性规则，输出命中规则号+依据。

规则基线：《数据集使用与F1规则规划_2026-10-18》§3.1；细化口径已登记接口说明 v0.2.1。
纪律：输出统称「异常候选」，不定性欺诈；同一输入可复算（无随机）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.db.connection import get_conn
from app.tools.bill_tools import DEMO_USER, validate_month
from app.tools.registry import l0_tool, result

RULES_VERSION = "P1-P4@2026-10-22"  # 规划文档基线 + 2026-10-22 P1 判据修正

# 阈值（单位：分）
#
# P1 判据 2026-10-22 修正（早期仅「同类别 30 天均值×3 且 ≥¥150」）：
#   问题：均值被同类别小额交易拉低后，正常的大额消费会成片越线。实测「¥30 快餐×20 + ¥210 聚餐」
#        小额均值仅 ¥31.25，×3 = ¥93.76，一顿 ¥210 的正常聚餐即被判异常；「¥150 日用品×12 +
#        ¥300 大件」同样命中。换到真实消费分布，几乎每笔稍大的正常消费都会被判「异常候选」。
#   连带：现金流预演默认剔除 P1-P4 命中交易再取均值，误报会直接让「月均支出」口径失真
#        （seed 数据下剔除后 2,753.19 元 vs 全口径 3,911.63 元，差 29.6%）。
#   修正：① 基准统计量由「均值」改为「P90 分位」——分位数抗离群，不被少量小额拉低；
#        ② 增加绝对下限 ¥500（P1_FLOOR_CENTS）——日常消费无论怎么算都不该进入「大额偏离」；
#        ③ 提高最小样本数到 8 笔——样本太少时分位数无统计意义，宁可不判。
P1_PERCENTILE = 0.90       # 同类别 30 天消费的 P90 分位（抗离群基准）
P1_MULTIPLE = 3.0          # 大额偏离倍数（相对 P90）
P1_FLOOR_CENTS = 50_000    # ¥500 绝对下限（原 ¥150 过低，日常消费会误报）
P1_MONTH_SHARE = 0.05      # 本笔须占当月支出 ≥5%（整体显著性，挡住同类别内的正常大件）
P2_NIGHT_AMOUNT = 20_000   # ¥200
P3_NEW_MERCHANT = 50_000   # ¥500
P4_WINDOW_COUNT = 5        # 1 小时内同商户笔数
P4_WINDOW_SUM = 20_000     # ¥200
HISTORY_DAYS = 30          # P1/P3 历史窗口（规划文档勘误 2：30 天口径）
P1_MIN_HISTORY = 8         # P90 最少样本数（原 3 笔，分位数在 3 个样本下无意义）

# 规则覆盖度声明：供界面如实披露「哪条规则在当前数据里不触发」
RULE_COVERAGE_NOTE = {
    "P1": "同类别 30 天 P90×3、单笔 ≥¥500、且占当月支出 ≥5%；需同类别历史 ≥8 笔。",
    "P2": "23:00–06:00 且单笔 ≥¥200。",
    "P3": "近 30 天该商户无记录且首笔 ≥¥500。【提示】演示数据中通常无自然样本，属规则完备性预留。",
    "P4": "1 小时内同商户 ≥5 笔小额且合计 ≥¥200。",
}


def _percentile(sorted_vals: list[int], q: float) -> float:
    """线性插值分位数（与 numpy.percentile 默认 method='linear' 一致，纯标准库实现）。"""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_vals[0])
    pos = (n - 1) * q
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac



def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _hour_in_night(dt: datetime) -> bool:
    # 23:00–06:00（06:00 不含）
    return dt.hour >= 23 or dt.hour < 6


def _fetch_history(cur, merchant: str, category: str, before: datetime) -> tuple[list, list]:
    """返回 (同商户记录, 同类别支出记录)，窗口 [before-30d, before)。"""
    lo = (before - timedelta(days=HISTORY_DAYS)).isoformat()
    m_rows = cur.execute(
        "SELECT txn_time, amount_cents FROM transactions"
        " WHERE user_id=? AND merchant_normalized=? AND direction='out'"
        "   AND txn_time >= ? AND txn_time < ?",
        (DEMO_USER, merchant, lo, before.isoformat())).fetchall()
    c_rows = cur.execute(
        "SELECT txn_time, amount_cents FROM transactions"
        " WHERE user_id=? AND category=? AND direction='out'"
        "   AND txn_time >= ? AND txn_time < ?",
        (DEMO_USER, category, lo, before.isoformat())).fetchall()
    return m_rows, c_rows


def _month_total_out(cur, month: str) -> int:
    """本月（YYYY-MM）全部流出合计（分），用于 P1 的整体显著性判据。"""
    y, m = int(month[:4]), int(month[5:7])
    hi = f"{y + 1}-01-01" if m == 12 else f"{y}-{m + 1:02d}-01"
    row = cur.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS s FROM transactions"
        " WHERE user_id=? AND direction='out' AND txn_time >= ? AND txn_time < ?",
        (DEMO_USER, f"{month}-01", hi)).fetchone()
    return row["s"] if row else 0


def _apply_rules(cur, txn: dict) -> list[dict]:
    """对单笔支出交易套用 P1-P4，返回命中规则列表。"""
    hits: list[dict] = []
    ts = _parse(txn["txn_time"])
    amt = txn["amount_cents"]
    merchant = txn["merchant_normalized"] or txn["counterparty"]
    m_hist, c_hist = _fetch_history(cur, merchant, txn["category"], ts)

    # P2 深夜交易
    if _hour_in_night(ts) and amt >= P2_NIGHT_AMOUNT:
        hits.append({"rule_id": "P2",
                     "basis": f"交易时间 {txn['txn_time'][11:16]} 位于 23:00–06:00，"
                              f"且单笔 {amt} 分 ≥ 阈值 {P2_NIGHT_AMOUNT} 分"})

    # P1 大额偏离（同类别 P90×3 且 ≥¥500），另需满足「占本月支出比重 ≥5%」
    #   双重收敛的理由：P90 只看同类别分布，挡不住「单笔 ¥3000 买家电」这类
    #   同类别内的正常大额。真实的大额异常（盗刷/误扣）通常同时是「本月级别的支出」，
    #   因此再加一道「占当月总支出 ≥P1_MONTH_SHARE」的整体显著性条件。
    if len(c_hist) >= P1_MIN_HISTORY:
        vals = sorted(r["amount_cents"] for r in c_hist)
        p90 = _percentile(vals, P1_PERCENTILE)
        if amt > p90 * P1_MULTIPLE and amt >= P1_FLOOR_CENTS:
            month_out = _month_total_out(cur, txn["txn_time"][:7])
            share = (amt / month_out) if month_out else 0.0
            if share >= P1_MONTH_SHARE:
                hits.append({"rule_id": "P1",
                             "basis": f"近30天同类别（{txn['category']}）{len(c_hist)} 笔的 P90 "
                                      f"{p90:.0f} 分，本笔 {amt} 分 > P90×{P1_MULTIPLE:g} 且 ≥ 绝对下限 "
                                      f"{P1_FLOOR_CENTS} 分（¥{P1_FLOOR_CENTS / 100:,.0f}）；"
                                      f"同时占当月支出 {share * 100:.1f}% ≥ {P1_MONTH_SHARE * 100:.0f}%"})

    # P3 陌生商户大额（近30天该商户无记录）
    if not m_hist and amt >= P3_NEW_MERCHANT:
        hits.append({"rule_id": "P3",
                     "basis": f"商户「{merchant}」近 {HISTORY_DAYS} 天无任何交易记录，"
                              f"首笔 {amt} 分 ≥ 阈值 {P3_NEW_MERCHANT} 分"})

    # P4 高频密集由 _compute_p4 滑动窗口统一计算（bills_anomaly 中并入）
    return hits


def _compute_p4(cur, month_txns: list[dict]) -> dict[str, dict]:
    """P4 高频密集：滑动窗口实现。

    对月内出现过的每个商户，取其全部小额支出（单笔 <¥20,000 分）按时间排序，
    两指针滑动 1 小时窗口；凡存在包含该交易的窗口满足「≥5 笔且合计 ≥¥20,000 分」，
    该交易即命中。返回 {txn_id: basis}。
    """
    merchants = {(t["merchant_normalized"] or t["counterparty"]) for t in month_txns}
    hits: dict[str, dict] = {}
    for merchant in merchants:
        rows = cur.execute(
            "SELECT txn_id, txn_time, amount_cents FROM transactions"
            " WHERE user_id=? AND merchant_normalized=? AND direction='out'"
            "   AND amount_cents < ?"
            " ORDER BY txn_time",
            (DEMO_USER, merchant, P2_NIGHT_AMOUNT)).fetchall()
        n = len(rows)
        i = 0
        window_sum = 0
        for j in range(n):
            window_sum += rows[j]["amount_cents"]
            # 收缩左端，保证窗口跨度 ≤ 1 小时
            while (_parse(rows[j]["txn_time"]) - _parse(rows[i]["txn_time"])) > timedelta(hours=1):
                window_sum -= rows[i]["amount_cents"]
                i += 1
            count = j - i + 1
            if count >= P4_WINDOW_COUNT and window_sum >= P4_WINDOW_SUM:
                basis = (f"1 小时内同商户「{merchant}」共 {count} 笔小额（单笔 <¥200），"
                         f"窗口合计 {window_sum} 分 ≥ {P4_WINDOW_SUM} 分")
                for k in range(i, j + 1):
                    hits[rows[k]["txn_id"]] = {"rule_id": "P4", "basis": basis}
    return hits


@l0_tool("bills.anomaly")
def bills_anomaly(month: str) -> dict:
    validate_month(month)
    lo = f"{month}-01"
    y, m = int(month[:4]), int(month[5:7])
    hi = (datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)).date().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    try:
        txns = cur.execute(
            "SELECT txn_id, txn_time, direction, amount_cents, counterparty,"
            " merchant_normalized, category FROM transactions"
            " WHERE user_id=? AND txn_time >= ? AND txn_time < ?"
            " ORDER BY txn_time",
            (DEMO_USER, lo, hi)).fetchall()
        month_txns = [dict(r) for r in txns]
        p4_hits = _compute_p4(cur, month_txns)
        items = []
        for r in month_txns:
            if r["direction"] != "out":
                continue
            hits = _apply_rules(cur, r)
            if r["txn_id"] in p4_hits:
                hits.append(p4_hits[r["txn_id"]])
            if hits:
                items.append({
                    "txn_id": r["txn_id"],
                    "txn_time": r["txn_time"],
                    "merchant": r["merchant_normalized"] or r["counterparty"],
                    "amount_cents": r["amount_cents"],
                    "hit_rules": hits,
                })
    finally:
        conn.close()
    # 按规则号统计本月命中数，供界面如实披露「哪条规则未触发」
    hit_count: dict[str, int] = {rid: 0 for rid in RULE_COVERAGE_NOTE}
    for it in items:
        seen = set()
        for h in it["hit_rules"]:
            if h["rule_id"] not in seen:
                seen.add(h["rule_id"])
                hit_count[h["rule_id"]] = hit_count.get(h["rule_id"], 0) + 1
    rule_coverage = [
        {"rule_id": rid, "hits": hit_count.get(rid, 0),
         "statement": RULE_COVERAGE_NOTE[rid],
         "silent": hit_count.get(rid, 0) == 0}
        for rid in RULE_COVERAGE_NOTE
    ]
    return result("bills.anomaly", {"month": month}, {
        "items": items,
        "rules_version": RULES_VERSION,
        "rule_coverage": rule_coverage,
        "disclaimer": "以上为异常候选（规则命中），不等于已证实欺诈。",
    })


@l0_tool("bills.anomaly.year")
def bills_anomaly_year(year: str) -> dict:
    """年度异常汇总。

    口径（关键，别改成「整年拼起来算一次」）：
    **逐月独立判定，再合并计数**。理由：P1 的基准是「同类别近 30 天 P90×3」，
    若把整年交易拼成一个池子重算，历史样本会把阈值整体抬高，每月的小异常全部沉底 ——
    结果仍然是「0 笔」，只是从「扫错月份」变成「扫了但判不出来」，更难发现。
    逐月算则与单月口径完全一致，可逐条复算。

    只汇总**有数据的月份**（缺失月份不补造），并如实披露覆盖范围。
    """
    from app.tools.bill_tools import bill_summary, bill_yearly

    yearly = bill_yearly(year)["data"]
    covered = yearly.get("covered_months") or []
    rows, total_hits, total_amount, total_txn = [], 0, 0, 0
    for m in covered:
        ra = bills_anomaly(m)["data"]
        items = ra["items"]
        n = len(items)
        amt = sum(i["amount_cents"] for i in items)
        txn = bill_summary(m)["data"]["txn_count"]
        rows.append({"month": m, "txn_count": txn, "hit_count": n, "hit_amount_cents": amt})
        total_hits += n
        total_amount += amt
        total_txn += txn

    return result("bills.anomaly.year", {"year": year}, {
        "year": year,
        "covered_months": covered,
        "missing_months": yearly.get("missing_months") or [],
        "by_month": rows,
        "total_hits": total_hits,
        "total_hit_amount_cents": total_amount,
        "total_txn_count": total_txn,
        "rules_version": RULES_VERSION,
        "method_note": "逐月独立判定后合并；不把全年交易拼池重算阈值（否则阈值被抬高、月内异常全部沉底）。",
        "disclaimer": "以上为异常候选（规则命中），不等于已证实欺诈。",
    })
