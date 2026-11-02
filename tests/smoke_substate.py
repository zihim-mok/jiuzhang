# -*- coding: utf-8 -*-
"""订阅状态一致性冒烟（2026-11-01）。

起因：用户取消两个订阅后，对话里的盘点表仍显示 4 笔、还给已取消的报了
「下次预计扣费 2026-11-05」，合计 518；而右侧台账面板（读 /subscriptions 并按
status 过滤）显示 2 笔生效、¥478。**同一屏两个数字对不上**，用户自然觉得「没取消」。

根因两条：
  1. 写路径：`subscriptions.cancel` 只改授权表状态，该订阅的 pending 续费提醒原地不动，
     于是「我有哪些要续费的」照样提醒一个已取消的订阅。
  2. 读路径：盘点表不分状态一律渲染、一律计入合计，并给已取消的算「下次预计扣费」。

本套锁住三件事：
  S1 取消后盘点表不得再给已取消的订阅预测扣费日，且不计入月合计；
  S2 盘点表「生效中·月合计」必须等于台账面板口径（= 生效中订阅月费之和）；
  S3 取消后续费提醒里不得再出现已取消的订阅，且库内提醒已置 dismissed。

跑法：.venv\\Scripts\\python.exe tests/smoke_substate.py
前置：库需为干净态（run_all_smokes 会自动 reset）。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["APP_ENV_FILE"] = os.path.join(ROOT, "tests", "fixtures", "__no_env__.env")

from fastapi.testclient import TestClient  # noqa: E402

from app.db.connection import get_conn  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS | " if ok else "FAIL | ") + name + ("" if ok else f"  <<< {detail}"), flush=True)


def _chat(msg: str, sid: str | None = None) -> dict:
    body = {"message": msg}
    if sid:
        body["session_id"] = sid
    r = client.post("/chat", json=body)
    return r.json()


def _table(j: dict) -> dict:
    return next((b for b in j.get("blocks", []) if b["type"] == "table"), {"rows": [], "columns": []})


def _kv(j: dict) -> dict:
    kv = next((b for b in j.get("blocks", []) if b["type"] == "kv"), {"pairs": []})
    return dict(kv.get("pairs") or [])


def _yuan(s: str) -> float:
    return float(re.sub(r"[^\d.]", "", s or "0") or 0)


def _panel_monthly() -> int:
    """/subscriptions 按与前端台账相同的口径（只看 status=active）算月费合计。"""
    d = client.get("/subscriptions").json()["data"]["items"]
    return sum(i["monthly_amount_cents"] for i in d if i["status"] == "active")


def main() -> int:
    sid = _chat("订阅怎么又扣了这么多？帮我看看")["session_id"]

    # ---- 取消前：4 笔在生效，且都给得出下次扣费日 ----
    j0 = _chat("订阅怎么又扣了这么多？帮我看看", sid)
    t0 = _table(j0)
    check("取消前盘点 4 笔且都有下次扣费日",
          len(t0["rows"]) == 4 and all(r[3] not in ("—", "-", "") for r in t0["rows"]),
          f"rows={len(t0['rows'])}")
    before_kv = _kv(j0)
    check("取消前月合计 = 台账口径",
          int(round(_yuan(before_kv.get("生效中·月合计", "0")) * 100)) == _panel_monthly(),
          f"表={before_kv.get('生效中·月合计')} / 台账={_panel_monthly()}")

    # ---- 走 L1 确认取消 ----
    j1 = _chat("健身的保留，其他两个取消", sid)
    card = j1.get("pending_confirmation") or {}
    if not card.get("confirmation_id"):
        # 本套依赖干净库（两个柔性可取消订阅都还在生效）。这里给出可执行的提示，
        # 而不是让它后面抛 KeyError —— 否则看着像功能坏了，其实只是没重置库。
        print("FAIL | 取消前出 L1 确认卡  <<< 未出卡：库不是干净态，"
              "请先运行 `python -m app.db.init_db --reset`（run_all_smokes 会自动做）")
        results.append(("取消前出 L1 确认卡", False, "库非干净态"))
        return _summary()
    check("取消前出 L1 确认卡", card.get("level") == "L1", f"card={card.get('confirmation_id')}")
    d = client.post(f"/confirmations/{card['confirmation_id']}/decision",
                    json={"decision": "approve"})
    check("approve 返回 executed", d.status_code == 200 and d.json().get("status") == "executed",
          f"HTTP {d.status_code}")

    # ---- S1/S2：取消后重新盘点 ----
    j2 = _chat("订阅怎么又扣了这么多？帮我看看", sid)
    t2 = _table(j2)
    cancelled_rows = [r for r in t2["rows"] if "已取消" in str(r[4])]
    live_rows = [r for r in t2["rows"] if "已取消" not in str(r[4])]
    check("S1 已取消的订阅不再给下次扣费日",
          len(cancelled_rows) == 2 and all(r[3] in ("—", "-", "") for r in cancelled_rows),
          f"已取消行={[(r[0], r[3]) for r in cancelled_rows]}")
    check("S1 生效中的订阅仍有下次扣费日", len(live_rows) == 2,
          f"生效行={[r[0] for r in live_rows]}")
    after_kv = _kv(j2)
    monthly = int(round(_yuan(after_kv.get("生效中·月合计", "0")) * 100))
    check("S2 取消后月合计排除已取消项", monthly == _panel_monthly(),
          f"表={monthly} 分 / 台账={_panel_monthly()} 分")
    check("S2 月合计确实是 47800 分（原 51800 − 已取消 4000）", monthly == 47800,
          f"实际 {monthly}")
    check("S2 kv 里另列了已取消金额", any("已取消" in k for k in after_kv),
          f"kv={list(after_kv)}")

    # ---- S3：续费提醒 ----
    j3 = _chat("我有哪些要续费的", sid)
    t3 = _table(j3)
    names = " ".join(str(r[0]) for r in t3["rows"])
    check("S3 提醒表里没有已取消的订阅",
          "星橙视频" not in names and "云速网盘" not in names, f"rows={names}")
    check("S3 提醒表只剩 2 条", len(t3["rows"]) == 2, f"rows={len(t3['rows'])}")
    notes = " ".join(b.get("text", "") for b in j3.get("blocks", []) if b["type"] == "note")
    check("S3 明确交代了已取消订阅不再提醒", "已取消的订阅" in notes, notes[:80])

    # ---- 库内：提醒真的被关掉了，不是只在展示层过滤 ----
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT r.sub_id, r.status FROM reminders r"
            " JOIN subscription_authorizations s ON s.sub_id = r.sub_id"
            " WHERE s.status = 'cancelled'").fetchall()
    finally:
        conn.close()
    check("S3 已取消订阅的提醒在库里是 dismissed",
          len(rows) == 2 and all(r["status"] == "dismissed" for r in rows),
          f"{[dict(r) for r in rows]}")

    return _exit_code()


def _summary() -> int:
    """提前退出的收尾（与正常路径同一份汇总口径）。"""
    return _exit_code()


def _exit_code() -> int:
    n = len(results)
    ok = sum(1 for _, o, _ in results if o)
    print(f"\n合计 {ok}/{n} 通过")
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(main())
