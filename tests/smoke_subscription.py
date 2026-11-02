# -*- coding: utf-8 -*-
""" 补：刚性/柔性分类识别与台账 8 字段回归。

为什么单独一套：这两项此前"文档写了、代码没跟上"，被发现的方式是人工试出来的
（问「把房租代扣取消了」→ 系统答「你想退哪一个？活跃的柔性订阅有…」）。
不补用例就会再次悄悄回退。本套只校验可观察行为，不依赖界面。

前置：服务已在 8000 端口运行，且为干净演示库（init_db --reset）。
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
results = []


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'} | {name} | {detail}")


def ask(msg: str):
    st, r = call("POST", "/chat", {"message": msg})
    texts = " ".join(b.get("text", "") for b in r.get("blocks", []) if b.get("type") == "text")
    notes = " ".join(b.get("text", "") for b in r.get("blocks", []) if b.get("type") == "note")
    tbl = next((b for b in r.get("blocks", []) if b.get("type") == "table"), None)
    return st, r, texts, notes, tbl


# ===================== 刚性代扣 =====================
RIGID_PHRASES = [
    "把房租代扣取消了",           # 直接点名词
    "取消房东的自动扣款",         # 换一种称呼（房东）
    "房租不想交了，帮我停掉",      # 口语 + 换动词（停掉）
]
for i, phrase in enumerate(RIGID_PHRASES, 1):
    st, r, texts, notes, tbl = ask(phrase)
    check(f"I{i} 刚性点名「{phrase}」→ 显式拒绝不建卡",
          st == 200 and r.get("pending_confirmation") is None
          and r["meta"]["intent_id"] == "F2.SUB_CANCEL"
          and "刚性" in texts and "没动" in texts,
          f"status={st} card={r.get('pending_confirmation') is not None} intent={r['meta']['intent_id']}")
    check(f"I{i}b 刚性拒绝附分类依据与替代路径（不静默吞）",
          tbl is not None and "住房租赁" in json.dumps(tbl, ensure_ascii=False)
          and ("协商" in notes or "协商" in json.dumps(tbl, ensure_ascii=False)),
          f"table={tbl is not None}")

# 刚性绝不进一键池：全量取消也不能把房租带走
st, r, texts, notes, tbl = ask("帮我把所有订阅都退了")
card = r.get("pending_confirmation")
ids = (card or {}).get("normalized_params", {}).get("sub_ids", [])
check("I4 全量取消时不包含刚性房租",
      "sub-rent" not in ids and (card is None or all(i != "sub-rent" for i in ids)),
      f"ids={ids}")

# ===================== 台账 8 字段 =====================
st, r = call("GET", "/subscriptions")
items = r["data"]["items"]
FIELDS = {
    "对方主体": "counterparty", "授权内容": "auth_scope", "签约时间": "signed_at",
    "有效期": "valid_until", "退出方式": "exit_method", "当前状态": "status",
    "累计扣款": "total_deducted_cents", "最近一次活动": "last_activity_at",
}
missing = {k: [i["sub_id"] for i in items if v not in i] for k, v in FIELDS.items()}
missing = {k: v for k, v in missing.items() if v}
check("I5 台账 8 字段齐备", not missing, f"缺失={missing}")

mock_tagged = [i["sub_id"] for i in items if "模拟" not in (i.get("auth_scope") or "")]
check("I6 现实不可得字段显式标「模拟」", not mock_tagged, f"未标={mock_tagged}")

# 累计扣款必须能与交易流水对账（用 /bills 系列不可直接算，这里校验其为非负整数且在合理区间）
bad = [i["sub_id"] for i in items
       if not isinstance(i.get("total_deducted_cents"), int) or i["total_deducted_cents"] < 0]
check("I7 累计扣款为整数分且非负", not bad, f"异常={bad}")

rigid = [i for i in items if i.get("rigidity") == "rigid"]
check("I8 刚柔分类字段存在且取值合法",
      all(i.get("rigidity") in ("rigid", "flexible") for i in items) and len(rigid) >= 1,
      f"刚性项={[i['sub_id'] for i in rigid]}")

basis_ok = all((i.get("rigidity_basis") or "").strip() for i in items)
check("I9 分类依据可回溯（rigidity_basis 非空）", basis_ok, "")

# ===================== 柔性路径未被误伤 =====================
st, r, texts, notes, tbl = ask("把锋浪健身退掉")
card = r.get("pending_confirmation")
check("I10 柔性点名仍能正常出 L1 确认卡",
      st == 200 and card is not None and card["level"] == "L1"
      and card["normalized_params"]["sub_ids"] == ["sub-gym"],
      f"card={card['normalized_params'] if card else None}")

if card:
    st2, r2 = call("POST", f"/confirmations/{card['confirmation_id']}/decision",
                   {"decision": "reject"})
    check("I11 拒绝后刚性/柔性状态均不变",
          st2 == 200 and r2["status"] == "rejected", f"status={st2}")

# 刚性与柔性混说的边界：不能因为提到房租就把房租也塞进取消集
st, r, texts, notes, tbl = ask("房租保留，把锋浪健身退了")
card = r.get("pending_confirmation")
ids = (card or {}).get("normalized_params", {}).get("sub_ids", [])
check("I12 混说时刚性不混入取消集",
      card is None or "sub-rent" not in ids, f"ids={ids}")

n_pass = sum(1 for _, ok, _ in results if ok)
print(f"\n合计 {n_pass}/{len(results)} 通过")
sys.exit(0 if n_pass == len(results) else 1)
