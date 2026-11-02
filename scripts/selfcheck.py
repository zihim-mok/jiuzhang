"""部署后自检（不联网、不调模型）。

独立成 .py 而不是塞在 PowerShell 的 here-string 里，原因：
  PowerShell 的 @"..."@ 对缩进和引号极其敏感，一旦手抖就整段语法错，
  而错误信息往往指向别处，排查成本高。放进 .py 后 PowerShell 只需一行调用。

用法（项目根目录，用虚拟环境解释器跑）：
    .venv\\Scripts\\python.exe scripts\\selfcheck.py
退出码：0=通过，1=有失败项
"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保能 import app（本文件在 scripts/ 下，须把项目根加进 sys.path）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(label: str, fn) -> None:
    try:
        detail = fn()
        print(f"  OK   {label}" + (f"  ({detail})" if detail else ""))
    except Exception as e:  # noqa: BLE001
        failures.append(f"{label}: {type(e).__name__} {e}")
        print(f"  失败  {label}  ->  {type(e).__name__}: {e}")


def _db_exists() -> str:
    db = ROOT / "app" / "data" / "banking_sim.db"
    if not db.exists():
        raise FileNotFoundError("数据库不存在，请先执行 python -m app.db.init_db --reset")
    return f"{db.stat().st_size // 1024} KB"


def _bill() -> str:
    from app.tools.bill_tools import bill_summary
    d = bill_summary("2026-09")["data"]
    if d["txn_count"] < 1:
        raise ValueError("账单为空")
    return f"2026-09 共 {d['txn_count']} 笔，支出 {d['total_out_cents']/100:,.2f} 元"


def _anomaly() -> str:
    from app.tools.anomaly_tools import bills_anomaly
    d = bills_anomaly("2026-09")["data"]
    items = d.get("items", [])
    if not items:
        raise ValueError("异常识别无命中（演示库应有 6 笔）")
    return f"命中 {len(items)} 笔，规则版本 {d.get('rules_version', '?')}"


def _forecast() -> str:
    from app.tools.forecast_tools import cashflow_forecast
    # 注意：工具函数统一返回 {tool, params, data, source} 包装，真实字段在 data 里
    d = cashflow_forecast(0, 3, False)["data"]
    if "baseline" not in d:
        raise ValueError("预演结果缺 baseline")
    return f"月均支出 {d['baseline']['monthly_avg_expense_cents']/100:,.2f} 元"


def _planner() -> str:
    from app.planning import stub_planner
    r = stub_planner.route("selftest-session", "这个月哪笔花得不对劲？")
    if not r.get("blocks"):
        raise ValueError("规划层无返回块")
    meta = r.get("meta", {})
    intent = meta.get("intent_id", "?")
    # 演示首句必须判到异常识别，落到 FALLBACK 说明规则覆盖有缺口
    if intent == "F4.FALLBACK":
        raise ValueError("演示首句被判为 FALLBACK，规则覆盖异常")
    return f"意图 {intent}（{meta.get('intent_source', '?')}）"


def _model() -> str:
    from app.core.config import get_config
    state = get_config().model.key_state
    # 这里不判失败：没填 key 是合法状态（替身模式），只如实报告
    return {"ok": "已配置真实 key", "placeholder": "占位符（替身模式）",
            "missing": "未配置（替身模式）"}.get(state, state)


print("=" * 56)
print("  九账算术 · 部署自检")
print("=" * 56)

check("演示数据库存在", _db_exists)
check("账单汇总可查", _bill)
check("异常识别可跑", _anomaly)
check("现金流预演可算", _forecast)
check("规划层可路由", _planner)
check("模型配置状态", _model)

print("-" * 56)
if failures:
    print(f"结果：{len(failures)} 项失败")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("结果：全部通过")
print("")
print("下一步：")
print("  真模型： .\\scripts\\serve_real.ps1 -Key sk-你的key")
print("  替身模式：.\\scripts\\serve_local.ps1")
print("  然后浏览器打开 http://localhost:8000/ui")
sys.exit(0)
