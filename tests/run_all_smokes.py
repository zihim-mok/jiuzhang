# -*- coding: utf-8 -*-
"""全套冒烟编排器：套间自动 reset + 重启服务。

背景：多套冒烟共用同一个 SQLite 库，首套改过的状态会污染后续套（曾出现 planning 先跑 →
transfer 的端到端用例拿不到确认卡）。本脚本把「干净库 + 重启服务」固化为每套的固定前置，
不再依赖人工 reset。

当前登记 12 套：planning / bills / transfer / graph / insights / injection / subscription /
fixture 依赖 8000 端口的后端服务，由本脚本负责起停；memory / concurrency / substate 为
in-process（TestClient + 线程），只共用库重置；llm 套自带服务编排（假模型 + 8010/8011
后端），也只共用库重置。

用法（项目根目录执行）：
  .venv\\Scripts\\python.exe tests\\run_all_smokes.py                        # 跑全部套
  .venv\\Scripts\\python.exe tests\\run_all_smokes.py --only planning,injection  # 跑指定套
  .venv\\Scripts\\python.exe tests\\run_all_smokes.py --keep-going            # 失败也继续（默认即继续）
  .venv\\Scripts\\python.exe tests\\run_all_smokes.py --fail-fast             # 首个失败即停
  .venv\\Scripts\\python.exe tests\\run_all_smokes.py --no-final-reset        # 结束后不恢复干净库

纪律：
  - 只跑脚本内登记的套件，不接受任意路径（防误跑）；
  - 每套前 `init_db --reset`，任何一步失败该套即记 FAIL 并如实报告，不掩盖；
  - 结束时恢复干净演示库（默认），保证紧接着演示的库指纹可复现。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# 套件登记表：id → (脚本文件, 说明, 是否依赖外部 8000 服务)
SUITES: dict[str, tuple[str, str, bool]] = {
    "planning": ("tests/smoke_planning_confirm.py", "规划层 /chat、多步任务链与确认端点", True),
    "bills": ("tests/smoke_bill_rules.py", "账单规则引擎（分类归一化 + 异常判定）", True),
    "transfer": ("tests/smoke_transfer_tasks.py", "转账 / 定时任务 / AA 分摊全链路", True),
    "graph": ("tests/smoke_graph.py", "状态图调度、多轮追问与兜底", True),
    "insights": ("tests/smoke_insights.py", "现金流预演与收支推断", True),
    "injection": ("tests/smoke_injection.py", "注入全族 A01-A10、数字来源校验与失败恢复", True),
    "subscription": ("tests/smoke_subscription.py", "代扣刚柔识别与授权台账 8 字段", True),
    "fixture": ("tests/smoke_fixture.py", "转账确认样本集（拒绝 / 过期 / 确认三类）", True),
    # 记忆模块：in-process TestClient + 无模型环境，零网络
    "memory": ("tests/smoke_memory.py", "记忆模块（表/双写/窗口/TTL/重置/降级）", False),
    # 确认单并发：in-process + 双线程
    "concurrency": ("tests/smoke_concurrency.py", "确认单并发与执行失败", False),
    # 订阅状态一致性：取消后盘点表不得再预测扣费、提醒不得再催已取消的订阅
    "substate": ("tests/smoke_substate.py", "订阅取消后的状态一致性（盘点/合计/提醒）", False),
    # 自带服务编排（假模型 8109 + 后端 8010/8011），不依赖外部 8000 服务
    "llm": ("tests/smoke_llm.py", "真模型接入层（白名单/回退/failover/密钥防护）", False),
}
DEFAULT_ORDER = ["planning", "bills", "transfer", "graph", "insights", "injection",
                 "subscription", "fixture", "memory", "concurrency", "substate", "llm"]

HEALTH_TIMEOUT = 40.0


def _log(msg: str) -> None:
    print(msg, flush=True)


def _health(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/healthz", timeout=1.5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _port_busy(base: str) -> bool:
    return _health(base)


def _reset_db() -> tuple[bool, str]:
    """重建模拟库；返回是否成功与最后一行摘要。"""
    proc = subprocess.run([PY, "-m", "app.db.init_db", "--reset"], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()
    return proc.returncode == 0, (tail[-1] if tail else "")


def _start_server(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )


def _stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_health(base: str, proc: subprocess.Popen) -> bool:
    deadline = time.time() + HEALTH_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:  # 服务自己挂了
            return False
        if _health(base):
            return True
        time.sleep(0.4)
    return False


def _run_suite(script: str) -> tuple[bool, str]:
    """返回 (是否通过, 末尾摘要)。"""
    proc = subprocess.run([PY, script], cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [ln for ln in out.strip().splitlines() if ln]
    tail = next((ln for ln in reversed(lines) if ln.startswith("合计")), "")
    if proc.returncode != 0 and not tail:
        tail = (lines[-1] if lines else f"exit={proc.returncode}")
    if proc.returncode != 0:
        _log(out)  # 失败时完整输出，便于定位
    return proc.returncode == 0, tail


def main() -> int:
    ap = argparse.ArgumentParser(description="全套冒烟编排（套间自动 reset + 重启服务）")
    ap.add_argument("--only", default="", help="逗号分隔的套件 id，如 planning,injection")
    ap.add_argument("--port", type=int, default=8000, help="后端端口，默认 8000")
    ap.add_argument("--fail-fast", action="store_true", help="首个失败即停")
    ap.add_argument("--no-final-reset", action="store_true", help="结束后不恢复干净库")
    args = ap.parse_args()

    if args.only:
        picked = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = [s for s in picked if s not in SUITES]
        if unknown:
            _log(f"[run_all_smokes] 未知套件：{unknown}；可用：{list(SUITES)}")
            return 2
    else:
        picked = list(DEFAULT_ORDER)

    base = f"http://127.0.0.1:{args.port}"
    if _port_busy(base):
        _log(f"[run_all_smokes] 端口 {args.port} 已有服务在跑，先停掉再执行"
             f"（避免本脚本误打到旧进程/旧库）。")
        return 2

    _log(f"[run_all_smokes] 套件 {len(picked)} 套：{', '.join(picked)}；端口 {args.port}；"
         f"解释器 {PY}")
    rows: list[tuple[str, str, bool, str]] = []
    for idx, sid in enumerate(picked, 1):
        script, desc, needs_server = SUITES[sid]
        _log(f"\n=== [{idx}/{len(picked)}] {sid} — {desc} ===")

        ok, db_note = _reset_db()
        if not ok:
            rows.append((sid, desc, False, f"reset 失败：{db_note}"))
            _log(f"[{sid}] reset 失败：{db_note}")
            if args.fail_fast:
                break
            continue
        _log(f"[{sid}] 库已重置 · {db_note}")

        if not needs_server:  # 自带服务编排的套件（如 llm）不占 8000
            passed, tail = _run_suite(script)
            rows.append((sid, desc, passed, tail))
            _log(f"[{sid}] {'PASS' if passed else 'FAIL'} · {tail}（自带服务编排）")
            if not passed and args.fail_fast:
                break
            continue

        proc = _start_server(args.port)
        try:
            if not _wait_health(base, proc):
                rows.append((sid, desc, False, "服务未在限期内就绪"))
                _log(f"[{sid}] 服务未就绪（{HEALTH_TIMEOUT:.0f}s 内）")
                if args.fail_fast:
                    break
                continue
            _log(f"[{sid}] 服务就绪，开始跑冒烟")
            passed, tail = _run_suite(script)
        finally:
            _stop_server(proc)
        rows.append((sid, desc, passed, tail))
        _log(f"[{sid}] {'PASS' if passed else 'FAIL'} · {tail}")
        if not passed and args.fail_fast:
            break

    if not args.no_final_reset:
        ok, note = _reset_db()
        _log(f"\n[run_all_smokes] 演示库已恢复干净态：{'成功' if ok else '失败'} · {note}")

    _log("\n===== 汇总 =====")
    for sid, desc, passed, tail in rows:
        _log(f"{'PASS' if passed else 'FAIL'} | {sid:<8} | {tail or desc}")
    n_pass = sum(1 for _, _, ok, _ in rows if ok)
    _log(f"合计 {n_pass}/{len(rows)} 套通过")
    return 0 if rows and n_pass == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
