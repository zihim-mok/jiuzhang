# -*- coding: utf-8 -*-
""" 冒烟：F1 规则引擎 N05 对账 + 9 类归一化（服务需已在 8000 端口运行）。"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
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


def rules_of(items, txn_id):
    for it in items:
        if it["txn_id"] == txn_id:
            return [h["rule_id"] for h in it["hit_rules"]]
    return None


# N05-1：深夜大额（2026-08）
st, r = call("GET", "/anomalies?month=2026-08")
items = r.get("data", {}).get("items", [])
rules = rules_of(items, "tx-anomaly-001")
check("N05-1 tx-anomaly-001 命中 P2（深夜大额）",
      st == 200 and rules and "P2" in rules, f"rules={rules}")

# N05-2：高频密集（2026-09，便利购 002-006）
st, r = call("GET", "/anomalies?month=2026-09")
items = r.get("data", {}).get("items", [])
p4_ids = {it["txn_id"] for it in items
          if any(h["rule_id"] == "P4" for h in it["hit_rules"])}
expected = {"tx-anomaly-002", "tx-anomaly-003", "tx-anomaly-004",
            "tx-anomaly-005", "tx-anomaly-006"}
check("N05-2 便利购 5 笔全部命中 P4", expected.issubset(p4_ids),
      f"p4={sorted(p4_ids)}")
dup = rules_of(items, "tx-anomaly-002")
check("N05-2b 重复扣款对保留在候选中（含 P4 依据）", dup is not None, f"rules={dup}")

# N05-3：P1 同类大额 + P3 新商户首笔（2026-10-22 新增样本 tx-anomaly-007）
# 此前 P1/P3 在 seed 中无任何自然样本，这两条规则从未被真正执行过。
rules7 = rules_of(items, "tx-anomaly-007")
check("N05-3 P1/P3 有自然样本并双双命中（同类 P90×3 + 新商户首笔 ¥980）",
      rules7 is not None and {"P1", "P3"}.issubset(set(rules7)),
      f"rules={rules7}")

# N05-3b：规则覆盖度如实披露（未触发的规则要标 silent，不能悄悄消失）
cov = {c["rule_id"]: c for c in r.get("data", {}).get("rule_coverage", [])}
check("N05-3b P1-P4 四条规则均入覆盖表，P1/P3/P4 本月非 silent",
      set(cov) >= {"P1", "P2", "P3", "P4"}
      and not cov["P1"]["silent"] and not cov["P3"]["silent"] and not cov["P4"]["silent"],
      f"silent={[k for k, v in cov.items() if v['silent']]}")

# 9 类归一化 + 合计断言
st, r = call("GET", "/bills/summary?month=2026-09")
d = r.get("data", {})
cats = [c["category"] for c in d.get("by_category", [])]
std9 = {"工资收入", "餐饮美食", "交通出行", "购物消费", "生活缴费",
        "休闲娱乐", "医疗教育", "转账往来", "其他支出"}
check("N02-9类 分类全部落在标准 9 类词汇表", set(cats).issubset(std9), str(cats))
check("N02-9类 分类合计=支出总额",
      sum(c["total_cents"] for c in d["by_category"]) == d["total_out_cents"],
      f"total={d.get('total_out_cents')}")
check("N02-9类 含覆盖提示字段（other_ratio/coverage_warning）",
      "other_ratio" in d and "coverage_warning" in d,
      f"other_ratio={d.get('other_ratio')}")

# /chat 异常意图端到端
st, r = call("POST", "/chat", {"message": "有没有异常消费"})
tr = r.get("tool_results", [])
check("F1.ANOMALY /chat 走真实规则引擎",
      st == 200 and any(t["tool"] == "bills.anomaly" for t in tr),
      f"tools={[t['tool'] for t in tr]}")

# 边界：非法月份
st, r = call("GET", "/anomalies?month=abc")
check("非法月份 422", st == 422, f"status={st}")

n_pass = sum(1 for _, ok, _ in results if ok)
print(f"\n合计 {n_pass}/{len(results)} 通过")
sys.exit(0 if n_pass == len(results) else 1)
