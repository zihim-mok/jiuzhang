# -*- coding: utf-8 -*-
""" 冒烟：现金流预演 + 个人版收支推断（服务需在 8000 端口，跑前须 --reset）。"""
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8000"


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    url = BASE + urllib.parse.quote(path, safe="/:?&=")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


results = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'} | {name} | {detail}")


def yuan(c: int) -> str:
    return f"{c / 100:,.2f}"


# ---------- T18 个人版收支推断 ----------
st, r = call("GET", "/insights/statement?month=2026-09")
d = r["data"]
# 交叉复算：与 bill.summary 同月支出一致（同表同口径）
st2, r2 = call("GET", "/bills/summary?month=2026-09")
check("S1-1 2026-09 四项推断（支出与 bill.summary 交叉一致）",
      st == 200 and d["income_cents"] == 1200000
      and d["expense_cents"] == r2["data"]["total_out_cents"] == 453896
      and d["balance_cents"] == d["income_cents"] - d["expense_cents"] == 746104,
      f"inc={d['income_cents']} exp={d['expense_cents']} bal={d['balance_cents']}")

nets = [p["net_cents"] for p in d["cashflow_by_period"]]
check("S1-2 现金流按旬分布合计=结余（可复算断言）", sum(nets) == d["balance_cents"]
      and len(nets) == 3, f"nets={nets}")

st, r = call("GET", "/insights/statement?month=2026-10")
d = r["data"]
check("S2 模拟当前月标注不完整（partial）且不参与基线",
      d["coverage"]["status"] == "partial" and "不完整" in d["coverage"]["note"]
      and d["expense_cents"] == 124776, f"exp={d['expense_cents']}")

st, r = call("GET", "/insights/statement?month=2026-03")
d = r["data"]
check("S3 数据期前月份显式无数据不补造",
      d["coverage"]["status"] == "no_data" and d["income_cents"] == 0
      and d["expense_cents"] == 0, d["coverage"]["note"][:30])

# ---------- T19 现金流预演 ----------
# 2026-10-22 口径调整（审阅 P0-2 连带修正）：exclude_anomalies 默认值由 true 改为 false，
# 默认走「全口径」（不静默剔除），剔除口径须显式传参。据此 S4-1/S4-2/S5 改为两条口径各测一遍。
# 复算：6 完整月。
#   全口径  total_out=2444979 → avg=407496；total_in=7200000 → avg=1200000；net=792504
#   剔除口径 total_out=1651919 → avg=275319；net=924681（剔除 P1-P4 共 7 笔）
FULL_AVG_EXP, FULL_NET = 407496, 792504
EXCL_AVG_EXP, EXCL_NET = 275319, 924681

st, r = call("GET", "/insights/forecast")
d = r["data"]
b, f = d["baseline"], d["forecast"]
check("S4-1 默认全口径基线=完整月均值（可复算，不静默剔除）",
      b["months"] == ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
      and b["monthly_avg_expense_cents"] == FULL_AVG_EXP
      and b["monthly_avg_income_cents"] == 1200000
      and b["monthly_avg_net_cents"] == FULL_NET
      and b["excluded_anomaly_count"] == 0,
      f"exp_avg={b['monthly_avg_expense_cents']} excluded={b['excluded_anomaly_count']}")

check("S4-1b 默认口径不静默剔除：仍披露剔除口径的对照差额（sensitivity）",
      d["sensitivity"]["compare"] == "剔除异常"
      and d["sensitivity"]["monthly_avg_expense_cents"] == EXCL_AVG_EXP
      # expense_diff_cents 为「当前口径 - 对照口径」（带符号）：全口径 - 剔除 = 少花的差额
      and d["sensitivity"]["expense_diff_cents"] == FULL_AVG_EXP - EXCL_AVG_EXP == 132177,
      f"compare={d['sensitivity']['compare']} alt={d['sensitivity']['monthly_avg_expense_cents']}")

check("S4-2 无干预预演三个月=基线净额",
      f["per_month"] == [{"month": "2026-11", "net_cents": FULL_NET},
                         {"month": "2026-12", "net_cents": FULL_NET},
                         {"month": "2027-01", "net_cents": FULL_NET}],
      f"per={[m['net_cents'] for m in f['per_month']]}")

check("S4-3 假设显式列出 + 模拟预测标注",
      len(d["assumptions"]) >= 4 and "模拟预测" in d["note"]
      and "置信区间" in d["note"], d["note"][:40])

st, r = call("GET", "/insights/forecast?weekly_adjust_cents=-50000&months_ahead=2")
d = r["data"]
effect = d["intervention"]["monthly_effect_cents"]  # -50000*52//12 = -216667
check("S5 干预参数联动（每周少 500 → 月净额 575837）",
      effect == -216667 and d["forecast"]["monthly_net_cents"] == FULL_NET + effect == 575837
      and len(d["forecast"]["per_month"]) == 2,
      f"effect={effect} net={d['forecast']['monthly_net_cents']}")

st, r = call("GET", "/insights/forecast?exclude_anomalies=true")
d = r["data"]
check("S6 剔除口径可显式切换（7 笔异常不计入均值）",
      d["baseline"]["monthly_avg_expense_cents"] == EXCL_AVG_EXP
      and d["baseline"]["monthly_avg_net_cents"] == EXCL_NET
      and d["baseline"]["excluded_anomaly_count"] == 7
      and d["sensitivity"]["compare"] == "全口径（不剔除）"
      and d["sensitivity"]["expense_diff_cents"] == EXCL_AVG_EXP - FULL_AVG_EXP == -132177,
      f"exp_avg={d['baseline']['monthly_avg_expense_cents']} excluded={d['baseline']['excluded_anomaly_count']}")

st, r = call("GET", "/insights/forecast?months_ahead=20")
check("S7 预演月数越界 422", st == 422
      and any("months_ahead" in str(x) for x in [r.get("detail", "")]), f"status={st}")

# ---------- /chat 意图接入 ----------
st, r = call("POST", "/chat", {"message": "看一下 2026-09 收支报表"})
tools = [t["tool"] for t in r["tool_results"]]
check("C1 收支报表意图走真实推断工具",
      st == 200 and "insights.personal_statement" in tools
      and any(b["type"] == "table" for b in r["blocks"]), f"tools={tools}")

st, r = call("POST", "/chat", {"message": "如果每周少花 500，帮我预演下未来三个月"})
tools = [t["tool"] for t in r["tool_results"]]
fc = r["tool_results"][0]["data"] if tools else {}
check("C2 预演意图带假设（每周少 500 → -50000 分口径）",
      st == 200 and "insights.cashflow_forecast" in tools
      and fc.get("intervention", {}).get("weekly_adjust_cents") == -50000
      and fc.get("forecast", {}).get("monthly_net_cents") == FULL_NET - 216667 == 575837,
      f"tools={tools} net={fc.get('forecast', {}).get('monthly_net_cents')}")

st, r = call("POST", "/chat", {"message": "帮我预演一下"})
check("C3 预演缺假设能追问（不硬算不猜）",
      st == 200 and r["tool_results"] == [] and r.get("pending_confirmation") is None
      and any("每周" in b.get("text", "") for b in r["blocks"] if b["type"] == "text"), "")

n_pass = sum(1 for _, ok, _ in results if ok)
print(f"\n合计 {n_pass}/{len(results)} 通过")
sys.exit(0 if n_pass == len(results) else 1)
