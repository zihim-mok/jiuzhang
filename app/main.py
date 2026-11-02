"""FastAPI 入口 — 工具层服务（模拟银行环境）。

四层架构中的工具层：所有查询/统计/写操作由确定性代码执行，
服务端校验权限与参数；金额、日期、余额以工具结果为准。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app import __version__
from app.core.business_policy import BusinessReadOnlyError, WRITES_MODE
from app.core.config import get_config
from app.core.logging import get_logger
from app.planning import blocks as B
from app.planning import stub_planner
from app.planning.sessions import get_session, new_session_id, reset_session
from app.services import confirmation_service, memory_service
from app.services.confirmation_service import ConfirmationError
from app.tools import bill_tools, subscription_write  # noqa: F401 (注册工具副作用)
from app.tools.anomaly_tools import bills_anomaly
from app.tools.contact_tools import contacts_search
from app.tools.forecast_tools import cashflow_forecast, personal_statement
from app.tools.insight_tools import insights_aa_split
from app.tools.transfer_tools import (TransferError, get_task, run_due_tasks,
                                      transfers_scheduled_list)

logger = get_logger("app.main")

app = FastAPI(
    title="AI Banking Agent 三场景原型 — 后端工具层",
    description="模拟银行环境 · 演示原型 · 所有数据为模拟数据，无真实资金操作",
    version=__version__,
)


@app.get("/healthz", tags=["meta"])
def healthz() -> dict:
    """健康检查。模型未配置时明确标注模拟替身模式，不虚构可用性。"""
    from app.planning.llm import status as llm_status
    cfg = get_config()
    return {
        "status": "ok",
        "version": __version__,
        "business_read_only": WRITES_MODE == "readonly",
        "writes_mode": WRITES_MODE,
        "environment": cfg.service.environment,
        "model_mode": "stub(模拟替身)" if cfg.model_stub_mode else "live",
        "model": llm_status(),
    }


class ProbeBody(BaseModel):
    text: str = "订阅怎么又扣了这么多？帮我看看"
    timeout_seconds: float | None = None


@app.post("/model/probe", tags=["meta"])
def model_probe(body: ProbeBody) -> JSONResponse:
    """模型可用性探测（只读、无副作用）：发一句文本给模型做意图理解，回结果与耗时。

    纪律：不回显模型原文、不回显密钥；未配置时如实返回 not_configured（不假装可用）。
    """
    from app.planning.llm import allowed_intents, classify_intent
    res = classify_intent(body.text, timeout=body.timeout_seconds)
    return JSONResponse({
        "ok": res.ok,
        "intent": res.intent if res.ok else None,
        "reason": res.reason or None,
        "provider": res.provider or None,
        "model_name": res.model_name or None,
        "allowed_intents": sorted(allowed_intents()) if res.ok is False and res.reason == "not_configured" else None,
    })


@app.on_event("startup")
def on_startup() -> None:
    cfg = get_config()
    logger.info(
        "服务启动 version=%s environment=%s model_mode=%s db=%s",
        __version__,
        cfg.service.environment,
        "stub" if cfg.model_stub_mode else "live",
        cfg.database.resolved_path,
    )
    # 记忆模块守卫（M1）：都只告警不阻断 ——
    # assert_ready 防「忘建表 → 记忆静默失效」，assert_single_user 防「记忆未按用户隔离」。
    memory_service.assert_ready()
    memory_service.assert_single_user()


# ---------- L0 只读端点（契约 v0.1 增补；错误统一转契约结构） ----------

def _ok(data: dict) -> dict:
    return {**data, "environment": get_config().service.environment}


def _err(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"code": code, "message": message}})


@app.exception_handler(sqlite3.OperationalError)
def _sqlite_operational(request, exc: sqlite3.OperationalError) -> JSONResponse:
    """数据库瞬时并发错误统一出口。

    `attempt to write a readonly database`（SQLITE_READONLY）在 WAL 并发写时会偶发出现
    ——实测两个线程同时写同一行，200 轮里 1 次。这类错误的特点是**语句没有生效**，
    所以可以安全地让用户重试；但如果不管，FastAPI 会回一句光秃秃的
    `Internal Server Error`，界面上看不出是「忙」还是「崩了」。
    这里明确成 503 + 可读文案，前端照着提示重试即可。
    """
    logger.warning("[db] 并发写冲突：%s %s", type(exc).__name__, exc)
    return _err("DB_BUSY", "数据库正忙（并发写冲突），本次请求未生效，请稍后重试。", 503)


@app.get("/accounts/me", tags=["L0"])
def accounts_me() -> dict:
    return _ok(bill_tools.accounts_me())


@app.get("/transactions", tags=["L0"])
def transactions(
    month: str | None = Query(default=None, description="YYYY-MM"),
    category: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    try:
        return _ok(bill_tools.transactions_query(month, category, limit, offset))
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e), 422)


@app.get("/bills/summary", tags=["L0"])
def bills_summary(month: str | None = None, year: str | None = None):
    try:
        if year:
            return _ok(bill_tools.bill_yearly(year))
        if month:
            return _ok(bill_tools.bill_summary(month))
        return _err("VALIDATION_ERROR", "须提供 month=YYYY-MM 或 year=YYYY", 422)
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e), 422)


@app.get("/anomalies", tags=["L0"])
def anomalies(
    month: str | None = Query(default=None, description="YYYY-MM，缺省=模拟当前月"),
    year: str | None = Query(default=None, description="YYYY，年度逐月汇总；与 month 同时给时以 month 为准"),
):
    """异常候选查询。

    新增 `year` 参数，与 `/bills/summary?year=` 的年度能力对齐；
    `month` 的缺省值改由 `clock.sim_today()` 推导，不再硬编码 2026-10。
    """
    from app.core.clock import sim_today
    from app.tools.anomaly_tools import bills_anomaly_year
    try:
        if year and not month:
            return _ok(bills_anomaly_year(year))
        return _ok(bills_anomaly(month or sim_today().strftime("%Y-%m")))
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e), 422)


@app.get("/contacts", tags=["L0"])
def contacts(query: str = Query(min_length=1)):
    try:
        return _ok(contacts_search(query))
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e), 422)


@app.get("/insights/statement", tags=["L0"])
def insights_statement(month: str | None = Query(default=None, description="YYYY-MM，缺省=最近完整月")):
    """个人版收支推断。缺省取模拟当前月的上一月（最近完整月）。"""
    try:
        if month is None:
            from app.core.clock import sim_today
            t = sim_today()
            month = f"{t.year - (t.month == 1):04d}-{(t.month - 2) % 12 + 1:02d}"
        return _ok(personal_statement(month))
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e), 422)


@app.get("/insights/forecast", tags=["L0"])
def insights_forecast(
    weekly_adjust_cents: int = Query(default=0, description="每周调整额（分，可为负）"),
    months_ahead: int = Query(default=3, ge=1, le=12),
    exclude_anomalies: bool = False,
):
    """现金流预演。确定性外推 + 参数化干预，假设显式。"""
    try:
        return _ok(cashflow_forecast(weekly_adjust_cents, months_ahead, exclude_anomalies))
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e), 422)


class AASplitRequest(BaseModel):
    total_cents: int
    people: int


@app.post("/aa/split", tags=["L0"])
def aa_split(req: AASplitRequest):
    try:
        return _ok(insights_aa_split(req.total_cents, req.people))
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e), 422)


@app.get("/tasks/{task_id}", tags=["L0"])
def task_status(task_id: str):
    task = get_task(task_id)
    if task is None:
        return _err("NOT_FOUND", f"任务 {task_id} 不存在", 404)
    return _ok({"task": task, "source": "tool"})


@app.get("/tasks", tags=["L0"])
def tasks_list():
    return _ok(transfers_scheduled_list())


class SchedulerRunRequest(BaseModel):
    # as_of 用于演示/验收 N16（到期执行），默认=模拟当前日
    as_of: str | None = None


@app.post("/scheduler/run", tags=["planning"])
def scheduler_run(req: SchedulerRunRequest):
    # 定时任务到期执行只可能产生转账类资金写入，仅在 all 档放行
    if WRITES_MODE != "all":
        return _err("BUSINESS_READ_ONLY", "当前业务策略禁止运行定时任务。", 403)
    from app.core.clock import sim_today
    as_of = req.as_of or sim_today().isoformat()
    try:
        date.fromisoformat(as_of)
    except ValueError:
        return _err("VALIDATION_ERROR", "as_of 须为 YYYY-MM-DD", 422)
    return _ok({"result": run_due_tasks(as_of), "source": "tool"})


@app.get("/subscriptions", tags=["L0"])
def subscriptions() -> dict:
    return _ok(bill_tools.subscriptions_list())


@app.get("/reminders", tags=["L0"])
def reminders() -> dict:
    return _ok(bill_tools.subscriptions_reminders())


# ---------- 规划层端点 ----------

class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str


@app.post("/chat", tags=["planning"])
def chat(req: ChatRequest):
    if not req.message.strip():
        return _err("VALIDATION_ERROR", "message 不能为空", 422)
    sid = req.session_id or new_session_id()
    # ① 载入滑动窗口，放进会话对象（不改 route / run_graph 的签名）。
    #    只影响模型分类那一支；规则命中路径不读它，18 条金句的可复现性不受影响。
    get_session(sid)["history"] = memory_service.load_window(sid)
    r = None
    reply_text = ""
    err = None
    try:
        r = stub_planner.route(sid, req.message)
    except ValueError as e:
        err = "VALIDATION_ERROR"
        return _err("VALIDATION_ERROR", str(e), 422)
    except Exception as e:  # noqa: BLE001 —— 500 也要留痕，否则界面有、记忆没有
        err = type(e).__name__
        raise
    else:
        reply_text = B.blocks_text(r["blocks"])
    finally:
        # ② 一个事务写 user + assistant。必须在 finally：
        #    只写在成功分支的话，dispatch 抛异常时 user 那半条会漏记，
        #    下一轮窗口里就出现「用户说了话、系统没答」的断口，指代更容易接错。
        memory_service.append_turn_pair(
            sid, req.message,
            ({**r, "reply": reply_text} if r is not None else None), err=err)
    return _ok({
        "session_id": sid,
        "reply": reply_text,
        "blocks": r["blocks"],
        "tool_results": r["tool_results"],
        "pending_confirmation": r["pending_confirmation"],
        # 接口契约的非破坏性增补：披露意图由规则还是真模型判定（不含密钥/金额/原文）
        "meta": r.get("meta", {}),
    })


class DecisionRequest(BaseModel):
    decision: str  # "approve" | "reject"


# 确认结果的展示块。
# 背景：/confirmations/{id}/decision 原本只返回 status+receipt，前端无从渲染凭证，
# 只能退化成一句「已执行。」，与「凭证可查」的验收要求不符。
# 这里把工具返回的凭证字段转成标准块（text/kv/note），旧页与新页均按 j.blocks 渲染。
_ACTION_CN = {
    "subscriptions.cancel": "取消订阅",
    "reminders.update": "修改续费提醒",
    # 原来这里写的是 "transfers.transfer"，
    # 而确认服务真正认的 action 是 "transfers.create" —— 键名对不上，
    # 于是凭证块里「操作」直接显示成 transfers.create 这种原始工具名，
    # 用户看到的是调试信息而不是人话。两个键都留，任一写法都能译。
    "transfers.create": "转账",
    "transfers.transfer": "转账",
    "transfers.scheduled.create": "创建定时转账",
    "transfers.scheduled.cancel": "取消定时转账",
    "aa.split": "AA 分摊",
}


def _decision_blocks(cid: str, result: dict) -> list[dict]:
    receipt = result.get("receipt") or {}
    action = receipt.get("action") or result.get("action") or ""
    action_cn = _ACTION_CN.get(action, action or "—")

    if result.get("status") != "executed":
        outcome = receipt.get("outcome") or "已跳过"
        return [
            B.text("已取消本次操作，账户与订阅状态未发生任何变更。"),
            B.kv([["确认单", cid], ["操作", action_cn], ["状态", "rejected"], ["结果", outcome]]),
        ]

    pairs: list[list] = [["确认单", cid], ["操作", action_cn], ["状态", "executed（模拟环境）"]]
    if receipt.get("receipt_id"):
        pairs.append(["凭证号", receipt["receipt_id"]])

    cancelled = receipt.get("cancelled") or []
    if isinstance(cancelled, list) and cancelled:
        names = [c.get("counterparty") or c.get("sub_id", "") if isinstance(c, dict) else str(c)
                 for c in cancelled]
        pairs.append(["已取消", "、".join(n for n in names if n)])

    skipped = receipt.get("skipped") or []
    if isinstance(skipped, list) and skipped:
        pairs.append(["已跳过", f"{len(skipped)} 项（详见工具结果）"])

    res = receipt.get("result") or {}
    if res.get("task_id"):
        pairs.append(["任务号", res["task_id"]])
    if res.get("next_run_date"):
        pairs.append(["下次执行", res["next_run_date"]])
    if res.get("txn_id"):
        pairs.append(["交易号", res["txn_id"]])
    if res.get("amount_cents") is not None:
        pairs.append(["金额", f"{res['amount_cents'] / 100:,.2f} 元"])

    blocks = [B.text("已按确认时的参数执行（模拟银行环境，无真实资金通道）。"), B.kv(pairs)]
    if receipt.get("record_warning"):
        # 钱动了但记账没落地：只披露、不掩饰，且必须给出「别重复点」的明确指示。
        blocks.append(B.note(receipt["record_warning"]))
    if res.get("task_id"):
        blocks.append(B.note("「已创建」≠「已到账」：定时任务尚未扣款，到期才按此参数执行，不会再向模型重新要参数。"))
    return blocks


@app.post("/confirmations/{confirmation_id}/decision", tags=["planning"])
def decide(confirmation_id: str, req: DecisionRequest):
    # v0.1.4：去掉按模式一刀切的 403，改由确认服务按"动作粒度"校验；
    # all 档全部放行（恢复原功能），cancel_only 仅放行退订卡，readonly 档在服务层拦截。
    try:
        r = confirmation_service.decide(confirmation_id, req.decision)
    except BusinessReadOnlyError as e:
        return _err("BUSINESS_READ_ONLY", str(e), 403)
    except ConfirmationError as e:
        # 执行期异常原本直接漏成 500「Internal Server Error」，
        # 界面上既看不到原因、确认单还停在 pending（可以无限重试）。
        # 现在业务类错误按语义给码，只有真正的服务端异常才是 500。
        status = {"CONFIRMATION_EXPIRED": 409, "NOT_FOUND": 404,
                  "VALIDATION_ERROR": 422, "INSUFFICIENT_FUNDS": 409,
                  "NEEDS_REVIEW": 409, "EXECUTION_ERROR": 500}.get(e.code, 400)
        return _err(e.code, e.message, status)
    blocks = _decision_blocks(confirmation_id, r)
    # 保留 status/receipt 原字段（兼容既有调用方），新增 blocks/reply 供前端渲染凭证
    return _ok({**r, "blocks": blocks, "reply": B.blocks_text(blocks)})


@app.post("/sessions/{session_id}/reset", tags=["planning"])
def session_reset(session_id: str):
    """重置会话：内存槽位 + 落库记忆一起清。

    只清内存是不够的 —— /chat 每轮都从 messages 表
    load_window，所以「新对话」之后模型照样看得见旧上下文。界面清空了而记忆没清，
    用户只会觉得莫名其妙。两处一起清，语义才和按钮上的字一致。
    """
    reset_session(session_id)
    cleared = memory_service.clear_session(session_id)
    return _ok({"reset": True, "session_id": session_id, "cleared_messages": cleared})


@app.get("/sessions/{session_id}/messages", tags=["planning"])
def session_messages(session_id: str, limit: int = Query(default=200, ge=1, le=200)):
    """回读该会话的对话轮次。

    与送给模型的滑动窗口**不是同一份**：这里是全量正序（上限 200），用于人看与留痕；
    窗口会按字符预算裁剪，只服务指代消解。
    超过 200 条时返回的是**最新** 200 条，并用 `truncated=true` 明说被截断了 ——
    静默少给一截比直接报错更难查。
    """
    items = memory_service.list_messages(session_id, limit)
    total = memory_service.count_messages(session_id)
    return _ok({"session_id": session_id, "count": len(items), "total": total,
                "truncated": total > len(items), "items": items})


# ---------- 原生网页前端 ----------
# 定位：唯一对外前端。纯静态 HTML + fetch 直打上面这些 HTTP 端点，任何能打开网页的环境都能用。
#   当前只保留 app.html 一套界面，另有 standalone.html（同版界面的离线单文件版，
#   内嵌夹具，无需后端）。

_WEB_DIR = Path(__file__).resolve().parent.parent / "frontend" / "web"


@app.get("/", include_in_schema=False)
@app.get("/ui", include_in_schema=False)
@app.get("/ui-new", include_in_schema=False)
def web_ui() -> FileResponse:
    """新版单文件前端（宣纸风格，六模块）。2026-10-30 起 / 与 /ui 均指向此页。

    来源 = frontend/web/standalone.html 剥离离线 fixtures/拦截器后的 app.html。
    /ui-new 保留为别名，避免此前分享出去的地址失效。
    """
    return FileResponse(_WEB_DIR / "app.html", media_type="text/html")

