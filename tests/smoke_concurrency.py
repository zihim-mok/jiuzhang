# -*- coding: utf-8 -*-
"""确认单并发与执行失败冒烟。

锁住三个「钱」上的缺陷，任何一个复发都必须让这套冒烟变红：

同一张确认单被两个请求同时 approve → 双扣款
     原实现「先 SELECT 看 pending，再执行工具，最后 UPDATE 成 approved」，
     中间隔着一次完整工具执行。两个并发请求都会读到 pending，然后各扣一次钱。
     实测 30 轮里 3 轮双扣（余额少 200 分、多一笔交易）。
     现在「pending → 最终态」是条件 UPDATE + rowcount 闸门，只有一个能改得动。

approve 时余额已不足 → HTTP 500 `Internal Server Error`，且确认单留在 pending
     用户可以无限次重试、每次都 500。现在业务异常映射为 4xx + 可读原因，
     并落成 failed（不退回 pending，避免「再点一次」变成双扣）。

`PRAGMA journal_mode = WAL` 原本每次 get_conn() 都执行
     WAL 切换要抢独占锁，并发下会抛 `attempt to write a readonly database`，
     漏成 500。现在只在建库时设一次，连接侧用 busy_timeout。

跑法：.venv\\Scripts\\python.exe tests/smoke_concurrency.py
前置：库需为干净态（run_all_smokes 会自动 reset）。
"""
from __future__ import annotations

import os
import sys
import threading
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["APP_ENV_FILE"] = os.path.join(ROOT, "tests", "fixtures", "__no_env__.env")

from fastapi.testclient import TestClient  # noqa: E402

from app.db.connection import get_conn  # noqa: E402
from app.main import app  # noqa: E402
from app.services import confirmation_service as cs  # noqa: E402
from app.tools import transfer_tools  # noqa: E402,F401  (注册 transfers.create)

client = TestClient(app, raise_server_exceptions=False)
results: list[tuple[str, bool, str]] = []

ROUNDS = 20          # 并发轮数。原缺陷 30 轮命中 3 次，20 轮足以复现；失败即说明回归。
AMOUNT = 100         # 分


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS | " if ok else "FAIL | ") + name + ("" if ok else f"  <<< {detail}"), flush=True)


def _balance() -> int:
    conn = get_conn()
    try:
        return conn.execute("SELECT balance_cents FROM accounts LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def _txn_count() -> int:
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    finally:
        conn.close()


def _contact() -> str:
    conn = get_conn()
    try:
        return conn.execute("SELECT contact_id FROM contacts LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def _new_card(amount_cents: int = AMOUNT) -> str:
    card = cs.create(user_id="u-demo", action="transfers.create", level="L2",
                     normalized_params={"contact_id": _contact(),
                                        "amount_cents": amount_cents,
                                        "request_id": f"req-{uuid.uuid4().hex[:12]}"},
                     summary="转账", impact=["余额减少"])
    return card["confirmation_id"]


def case_d11_concurrent_approve() -> None:
    """两个线程同时 approve 同一张卡：只能扣一次钱、只能成一笔交易。"""
    doubles = 0
    both_ok = 0
    for _ in range(ROUNDS):
        cid = _new_card()
        b0, t0 = _balance(), _txn_count()
        out: list[str] = []
        bar = threading.Barrier(2)

        def worker():
            bar.wait()
            try:
                cs.decide(cid, "approve")
                out.append("executed")
            except Exception as e:  # noqa: BLE001
                out.append(type(e).__name__)

        ts = [threading.Thread(target=worker) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        spent, added = b0 - _balance(), _txn_count() - t0
        if spent >= AMOUNT * 2 or added >= 2:
            doubles += 1
        if out.count("executed") > 1:
            both_ok += 1

    check("并发 approve 不双扣款", doubles == 0,
          f"{ROUNDS} 轮中有 {doubles} 轮扣了两次钱")
    check("并发 approve 只有一个返回 executed", both_ok == 0,
          f"{ROUNDS} 轮中有 {both_ok} 轮两个请求都以为成功")


def case_d11_late_click() -> None:
    """已执行过的卡再点一次：必须 409，且金额与笔数不变。"""
    cid = _new_card()
    r1 = client.post(f"/confirmations/{cid}/decision", json={"decision": "approve"})
    b1, t1 = _balance(), _txn_count()
    r2 = client.post(f"/confirmations/{cid}/decision", json={"decision": "approve"})
    check("二次点击返回 409", r1.status_code == 200 and r2.status_code == 409,
          f"第一次 {r1.status_code} / 第二次 {r2.status_code}")
    check("二次点击不再扣钱", _balance() == b1 and _txn_count() == t1,
          f"余额 {b1}->{_balance()} 笔数 {t1}->{_txn_count()}")


def case_d11_expire_does_not_clobber_executing() -> None:
    """expire_pending 不能把已执行的卡改成 expired（钱的记录必须对得上）。"""
    cid = _new_card()
    cs.decide(cid, "approve")
    changed = cs.expire_pending(cid, reason="smoke")
    conn = get_conn()
    try:
        st = conn.execute("SELECT status FROM confirmations WHERE confirmation_id=?",
                          (cid,)).fetchone()["status"]
    finally:
        conn.close()
    check("已执行的卡不会被置为 expired", changed is False and st == "approved",
          f"changed={changed} status={st}")


def case_d12_insufficient_funds() -> None:
    """余额不足时 approve：4xx + 可读原因 + 卡落 failed（不能留在 pending 让人反复点）。"""
    cid = _new_card(amount_cents=99_999_999)
    r = client.post(f"/confirmations/{cid}/decision", json={"decision": "approve"})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    conn = get_conn()
    try:
        st = conn.execute("SELECT status FROM confirmations WHERE confirmation_id=?",
                          (cid,)).fetchone()["status"]
    finally:
        conn.close()
    check("余额不足不再 500", r.status_code != 500, f"HTTP {r.status_code}")
    check("余额不足返回业务错误码", body.get("error", {}).get("code") == "INSUFFICIENT_FUNDS",
          f"body={str(body)[:120]}")
    check("余额不足的卡落 failed 而非 pending", st == "failed", f"status={st}")

    # 再点一次也不能变成 500；且不应产生任何扣款
    b1, t1 = _balance(), _txn_count()
    r2 = client.post(f"/confirmations/{cid}/decision", json={"decision": "approve"})
    check("失败卡重复点击不扣款",
          r2.status_code != 200 and _balance() == b1 and _txn_count() == t1,
          f"HTTP {r2.status_code} 余额 {b1}->{_balance()}")


def case_d12_reject_still_clean() -> None:
    """reject 路径不能被上面的改动带坏。"""
    cid = _new_card()
    b1 = _balance()
    r = client.post(f"/confirmations/{cid}/decision", json={"decision": "reject"})
    conn = get_conn()
    try:
        st = conn.execute("SELECT status FROM confirmations WHERE confirmation_id=?",
                          (cid,)).fetchone()["status"]
    finally:
        conn.close()
    check("reject 返回 200 且状态为 rejected",
          r.status_code == 200 and st == "rejected", f"HTTP {r.status_code} status={st}")
    check("reject 不动余额", _balance() == b1, f"余额 {b1}->{_balance()}")


def case_action_label_is_chinese() -> None:
    """凭证块里的「操作」必须是中文，不能把 transfers.create 这种工具名甩给用户。

    2026-10-30 端到端跑真卡时发现：_ACTION_CN 里写的是 transfers.transfer，
    而确认服务认的 action 是 transfers.create，键名对不上 → 界面显示原始工具名。
    """
    import re as _re
    cid = _new_card()
    r = client.post(f"/confirmations/{cid}/decision", json={"decision": "approve"})
    kv = next((b for b in (r.json().get("blocks") or []) if b.get("type") == "kv"), {})
    pairs = dict(kv.get("pairs") or [])
    label = str(pairs.get("操作", ""))
    check("凭证块的「操作」是中文而非工具名",
          bool(label) and not _re.search(r"[A-Za-z_]", label),
          f"操作显示为 {label!r}")


def case_d13_no_wal_pragma_per_conn() -> None:
    """每个连接重设 journal_mode 是 的根因，锁住这个写法不许回来。"""
    src = open(os.path.join(ROOT, "app", "db", "connection.py"), encoding="utf-8").read()
    # 注意只匹配真正的语句调用：注释里提到 journal_mode 是允许的（那是在解释为什么不能设）
    check("get_conn 不再每次执行 journal_mode 语句",
          'execute("PRAGMA journal_mode' not in src,
          "connection.py 又出现了 PRAGMA journal_mode 调用，并发下会偶发 readonly database")
    check("get_conn 设置了 busy_timeout", "busy_timeout" in src, "缺少 busy_timeout")


def main() -> int:
    case_d13_no_wal_pragma_per_conn()
    case_d11_late_click()
    case_d11_concurrent_approve()
    case_d11_expire_does_not_clobber_executing()
    case_d12_insufficient_funds()
    case_d12_reject_still_clean()
    case_action_label_is_chinese()

    n = len(results)
    ok = sum(1 for _, o, _ in results if o)
    print(f"\n合计 {ok}/{n} 通过")
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(main())
