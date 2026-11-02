# -*- coding: utf-8 -*-
"""夹具对齐冒烟：用转账确认样本集（transfer_confirm.csv，1958 条）驱动确认服务的行为验证。

口径纪律：
- 数据集只作测试夹具（来源与采样方式见 fixtures/transfer_confirm_cases.json），不替换演示 seed；
- 行为对齐点：①拒绝后状态/余额不变（B09/B10 族）②过期后表态 → CONFIRMATION_EXPIRED（B11 族）
  ③金额线：数据集 ≥500 元强制 L2，本项目全量 L2（更保守）；
- TTL 数值差异如实记录：数据集 5 分钟 vs 本项目 30 分钟（语义一致，方向更宽），不冒充一致。
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8000"
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "transfer_confirm_cases.json")


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


def balance():
    st, r = call("GET", "/accounts/me")
    return r["data"]["accounts"][0]["balance_cents"]


fx = json.load(open(FIXTURE, encoding="utf-8"))

# X1 数据集统计与 统计口径一致（过期 129 / 拒绝 87）
check("X1 夹具统计与评审口径一致（total/status）",
      fx["total"] == 1958 and fx["status_counts"] == {"已确认": 1742, "已过期": 129, "用户拒绝": 87},
      str(fx["status_counts"]))

b0 = balance()
ok_reject = True
for i, c in enumerate(fx["cases"]["rejected"], 1):
    st, r = call("POST", "/chat", {"message": f"给张伟转 {c['amount']}"})
    card = r.get("pending_confirmation")
    if not (card and card["action"] == "transfers.create" and card["level"] == "L2"):
        ok_reject = False
        print(f"  rejected#{i} 出卡异常: {card}")
        continue
    st2, r2 = call("POST", f"/confirmations/{card['confirmation_id']}/decision",
                   {"decision": "reject"})
    if not (st2 == 200 and r2["status"] == "rejected"):
        ok_reject = False
        print(f"  rejected#{i} 表态异常: {st2}")
check("X2 数据集拒绝样本×10 全部走 L2 卡且拒绝后余额不变（B09/B10 族）",
      ok_reject and balance() == b0, f"bal={balance()}=={b0}")

# X2b 保守口径：数据集 <500 元免确认段，本项目仍强制 L2
st, r = call("POST", "/chat", {"message": "给张伟转 100"})
card = r.get("pending_confirmation")
st2, r2 = (call("POST", f"/confirmations/{card['confirmation_id']}/decision",
                {"decision": "reject"}) if card else (None, None))
check("X2b 金额线对齐：本项目全量 L2（<500 元也须确认，比数据集口径更保守）",
      card is not None and card["level"] == "L2" and st2 == 200
      and balance() == b0, f"card={card is not None}")

# X3 数据集过期样本×10：确认过期后表态 → 409 CONFIRMATION_EXPIRED（B11 族）
from app.db.connection import get_conn
from app.planning.sessions import now8
ok_exp = True
for i, c in enumerate(fx["cases"]["expired"], 1):
    st, r = call("POST", "/chat", {"message": f"给张伟转 {c['amount']}"})
    card = r.get("pending_confirmation")
    if not card:
        ok_exp = False
        print(f"  expired#{i} 出卡异常")
        continue
    conn = get_conn()  # 【故障注入】人为推进过期时刻（对齐数据集 5 分钟 TTL 语义）
    try:
        conn.execute("UPDATE confirmations SET expires_at=? WHERE confirmation_id=?",
                     ((now8() - timedelta(seconds=1)).isoformat(),
                      card["confirmation_id"]))
        conn.commit()
    finally:
        conn.close()
    st2, r2 = call("POST", f"/confirmations/{card['confirmation_id']}/decision",
                   {"decision": "approve"})
    if not (st2 == 409 and r2["error"]["code"] == "CONFIRMATION_EXPIRED"):
        ok_exp = False
        print(f"  expired#{i} 过期表态异常: {st2} {r2}")
check("X3 数据集过期样本×10 过期后表态全 409（B11 族，故障注入法）",
      ok_exp and balance() == b0, f"bal={balance()}")

# X4 数据集已确认样本×3：approve 闭环执行（金额 550.68/504.00/518.56，合计 1573.24 元）
bal = b0
ok_ok = True
for i, c in enumerate(fx["cases"]["confirmed_small"], 1):
    st, r = call("POST", "/chat", {"message": f"给张伟转 {c['amount']}"})
    card = r.get("pending_confirmation")
    if not card:
        ok_ok = False
        print(f"  confirmed#{i} 出卡异常")
        continue
    st2, r2 = call("POST", f"/confirmations/{card['confirmation_id']}/decision",
                   {"decision": "approve"})
    txn = r2.get("receipt", {}).get("result", {}).get("txn_id")
    bal -= c["amount_cents"]
    if not (st2 == 200 and r2["status"] == "executed" and txn and balance() == bal):
        ok_ok = False
        print(f"  confirmed#{i} 执行异常: {st2} bal={balance()} 期望={bal}")
check("X4 数据集确认样本×3 approve 全闭环（凭证+余额逐笔对账）",
      ok_ok and balance() == bal, f"bal={balance()}=={bal}")

# X5 TTL 口径差异如实标注（数据集 5 分钟 vs 本项目 30 分钟，语义一致）
from app.services.confirmation_service import TTL_DEFAULT_MIN
check("X5 TTL 差异如实标注（30 分钟>数据集 5 分钟，方向更宽）",
      TTL_DEFAULT_MIN == 30 and fx["ttl_note"].startswith("数据集 TTL=5 分钟")
      and all(c["ttl_minutes"] == 5.0 for c in fx["cases"]["expired"]),
      f"TTL={TTL_DEFAULT_MIN}min")

n_pass = sum(1 for _, ok, _ in results if ok)
print(f"\n合计 {n_pass}/{len(results)} 通过")
sys.exit(0 if n_pass == len(results) else 1)
