"""会话记忆（M1）：messages 表 + 滑动窗口。

边界（2026-10-30 决策）：记忆归属 = session_id，**不加 user_id 列**。
单用户演示画像，身份由服务端 DEMO_USER 固定注入；session_id 已是隔离边界。
若将来接入第二个用户：必须改为 (user_id, session_id) 双键并在 /chat 校验会话归属。
assert_single_user() 负责把这件事变成启动期可见的告警，而不是沉默的错配。

三条纪律（违反任何一条都会把记忆变成缺陷）：
1. 记忆不产生授权 —— messages 里的「已确认」永远不算确认，授权只认 confirmations 状态机。
2. 记忆不是事实源 —— 金额/余额/日期一律回工具取，本模块返回值不得参与任何计算。
3. assistant 消息不可信 —— 回灌时用 <history> 包裹并声明非指令（防二次注入）。
"""
from __future__ import annotations

import json
import logging

from app.db.connection import get_conn

log = logging.getLogger(__name__)

CHAR_BUDGET = 1200              # 中文 1 字≈1 token 粗估
KEEP_LAST_TURNS = 2             # 超预算也保留末 2 轮（指代总在相邻轮）
MAX_PER_SESSION = 200
TTL_HOURS = 24


def append_turn_pair(session_id: str, user_text: str, result: dict | None,
                     err: str | None = None) -> None:
    """一个事务写 user + assistant 两条。永不抛：记忆是旁路，不能拖垮主链路。

    为什么成一个事务：半条 (只有 user 没有 assistant) 会让下一轮的窗口里出现
    「用户说了话、系统没答」的断口，指代消解反而更容易接错。
    """
    if not session_id or not user_text:
        return
    intent, tools, reply = None, None, ""
    if result:
        intent = (result.get("meta") or {}).get("intent_id")
        tools = [t.get("tool") for t in (result.get("tool_results") or []) if t.get("tool")]
        reply = result.get("reply")
        if reply is None:  # route() 返回的是 blocks，reply 由调用方渲染；这里兜一层
            reply = ""
    elif err:
        reply = f"[未生成回复：{err}]"
    try:
        conn = get_conn()
        try:
            cur = conn.cursor()
            for role, text, it in (("user", user_text, None), ("assistant", reply, intent)):
                cur.execute(
                    "INSERT INTO messages(session_id, role, content, intent_id,"
                    " tool_names, created_at) VALUES(?,?,?,?,?,datetime('now'))",
                    (session_id, role, text[:4000], it,
                     json.dumps(tools, ensure_ascii=False) if (tools and role == "assistant") else None))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:      # 旁路失败不得影响已完成的业务动作
        log.warning("[memory] 写入失败（不影响业务）：%s", type(e).__name__)


def load_window(session_id: str) -> list[dict]:
    """取滑动窗口，返回 [{"role","content"}]。出错一律降级为无记忆。

    超预算的丢弃策略是「从最旧开始丢」，但**末 2 轮无条件保留** ——
    指代（「那个」「剩下的」）几乎总发生在相邻轮，丢末轮等于把记忆做坏。
    """
    if not session_id:
        return []
    try:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id=?"
                " AND created_at > datetime('now', ?) ORDER BY id DESC LIMIT ?",
                (session_id, f"-{TTL_HOURS} hours", MAX_PER_SESSION)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        log.warning("[memory] 读取失败（降级为无记忆）：%s", type(e).__name__)
        return []
    rows = list(reversed(rows))
    keep_n = KEEP_LAST_TURNS * 2
    tail = rows[-keep_n:] if len(rows) > keep_n else rows
    head = rows[:-keep_n] if len(rows) > keep_n else []
    kept, used = list(tail), sum(len(r["content"] or "") for r in tail)
    for r in reversed(head):                      # 从最旧开始丢
        n = len(r["content"] or "")
        if used + n > CHAR_BUDGET:
            break
        kept.insert(0, r)
        used += n
    # 砍掉开头的孤立 assistant。
    # 预算按字符数截断时，最后塞进去的那一条可能正好是一轮的**回复**而没有它的**问题**，
    # 于是窗口以 assistant 开头 —— 模型看到「凭空一句答复」，指代反而更容易接错。
    # 设计要求「无孤儿 assistant」，之前那次测试是尾部长度为偶数才侥幸通过。
    while kept and kept[0]["role"] == "assistant":
        kept.pop(0)
    return [{"role": r["role"], "content": r["content"] or ""} for r in kept]


def list_messages(session_id: str, limit: int = 200) -> list[dict]:
    """按时间正序回读该会话的轮次（供 GET /sessions/{id}/messages）。

    与 load_window 的区别：这里**不按字符预算裁剪**，用于人看与留痕；窗口只服务模型上下文。

    原实现是 `ORDER BY id ASC LIMIT ?`，超过 200 条时
    返回的是**最早**的 200 条，最新那几十轮被无声丢掉 —— 回读接口声称「全部轮次」，
    实际给了最旧的。改成取最新 cap 条再正序返回；是否截断由调用方用 count_messages 判断并披露。
    """
    if not session_id:
        return []
    cap = max(1, min(int(limit), MAX_PER_SESSION))
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, role, content, intent_id, tool_names, created_at FROM messages"
            " WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, cap)).fetchall()
    finally:
        conn.close()
    out = []
    for r in reversed(rows):
        try:
            tools = json.loads(r["tool_names"]) if r["tool_names"] else []
        except (ValueError, TypeError):
            tools = []
        out.append({"id": r["id"], "role": r["role"], "content": r["content"],
                    "intent_id": r["intent_id"], "tool_names": tools,
                    "created_at": r["created_at"]})
    return out


def count_messages(session_id: str) -> int:
    """该会话的总条数（不受 MAX_PER_SESSION 限制），用于披露回读是否被截断。"""
    if not session_id:
        return 0
    try:
        conn = get_conn()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?",
                                    (session_id,)).fetchone()[0])
        finally:
            conn.close()
    except Exception as e:
        log.warning("[memory] 计数失败：%s", type(e).__name__)
        return 0


def clear_session(session_id: str) -> int:
    """删除该会话的全部记忆（供「新对话 / reset」调用），返回删除条数。

    此前 reset 只清内存会话对象，messages 表不动，
    而 /chat 每轮都从表里 load_window —— 结果是用户点了「新对话」，
    模型下一条**仍然看得见旧上下文**（实测：reset 后「那 9 月呢」仍被判成 ANOMALY）。
    界面上清空了，记忆里没清，属于最容易被当成「玄学」的那类不一致。
    """
    if not session_id:
        return 0
    try:
        conn = get_conn()
        try:
            cur = conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()
    except Exception as e:
        log.warning("[memory] 清除失败（不影响 reset 本身）：%s", type(e).__name__)
        return 0


def assert_ready() -> None:
    """启动自检：messages 表在不在。

    为什么需要：本模块所有异常都被吞成 warning（记忆是旁路，不能拖垮主链路），
    于是「忘了跑建表脚本」的症状是**记忆完全失效但服务一切正常** ——
    正是设计稿担心的「接了记忆但感觉没变」。这里把它喊到启动日志里，只告警不阻断。
    """
    try:
        conn = get_conn()
        try:
            conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log.warning("[memory] messages 表不可用（%s）：记忆功能当前为关闭状态。"
                    "已有库请执行 app/db/schema.sql 建表（勿用 --reset，会清演示数据）。",
                    type(e).__name__)


def assert_single_user() -> None:
    """单用户守卫：users 表多于 1 行即告警（记忆尚未按用户隔离）。"""
    try:
        conn = get_conn()
        try:
            n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        return
    if n > 1:
        log.warning("[memory] users 表有 %d 行，但记忆仅按 session 隔离、未按用户隔离", n)
