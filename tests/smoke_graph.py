# -*- coding: utf-8 -*-
""" 冒烟：LangGraph 调度与任务链——意图分发/多轮追问/超范围应答/异常兜底（服务需在 8000 端口）。"""
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
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read().decode())


results = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'} | {name} | {detail}")


# G1 图链路端到端：/chat 经 sync→classify→dispatch 返回块结构+工具序列
st, r = call("POST", "/chat", {"message": "订阅怎么又扣了这么多？帮我看看"})
check("G1 状态图端到端（块+工具序列+意图落账）",
      st == 200 and any(b["type"] == "table" for b in r["blocks"])
      and r["tool_results"] and r["tool_results"][0]["tool"] == "subscriptions.list",
      f"tools={[t['tool'] for t in r['tool_results']]}")

# G2 多轮澄清（槽位）：转账缺收款人 → 追问；补答后金额追问
st, r = call("POST", "/chat", {"message": "我要转账"})
texts1 = " ".join(b.get("text", "") for b in r["blocks"] if b["type"] == "text")
st2, r2 = call("POST", "/chat", {"session_id": r["session_id"], "message": "给张伟转"})
texts2 = " ".join(b.get("text", "") for b in r2["blocks"] if b["type"] == "text")
check("G2 模糊指令能追问（缺收款人→缺金额两轮衔接）",
      "转给谁" in texts1 and "转多少" in texts2 and r2.get("pending_confirmation") is None,
      f"q1={'转给谁' in texts1} q2={'转多少' in texts2}")

# G3 超范围标准应答：表外输入走 fallback 分支，不调任何工具
st, r = call("POST", "/chat", {"message": "帮我订一份鲜花蛋糕"})
check("G3 越界请求明确反馈（fallback 不调工具）",
      st == 200 and r["tool_results"] == [] and r.get("pending_confirmation") is None
      and any("暂时做不了" in b.get("text", "") for b in r["blocks"] if b["type"] == "text"),
      f"tools={len(r['tool_results'])}")

# G4 异常兜底分支可达：直调 _dispatch 注入异常 → 如实报错块、不产卡、清槽位
from app.planning import stub_planner as SP
from app.planning.sessions import get_session
_orig = SP._HANDLERS["F1.SAVINGS"]
SP._HANDLERS["F1.SAVINGS"] = lambda s, t: 1 / 0
s = get_session("g4-test")
s["pending_slots"] = {"intent": "F1.SAVINGS", "amount_cents": 1}
blks, trs, pending = SP._dispatch(s, "省多少", "F1.SAVINGS")
SP._HANDLERS["F1.SAVINGS"] = _orig
check("G4 失败兜底雏形（如实报错不补造+清槽位）",
      pending is None and trs == [] and "执行出错了" in blks[0]["text"]
      and s.get("pending_slots") is None,
      f"first={blks[0]['text'][:20]}…")

# G5 会话状态跨请求一致：槽位遗留时 reset 应清空（reset 后"转 200"须重新追问收款人而非直接产卡）
st, r = call("POST", "/chat", {"message": "我要转账"})
sid = r["session_id"]
st, r = call("POST", "/chat", {"session_id": sid, "message": "给张伟转"})  # 产生金额槽位
st, r = call("POST", "/sessions/" + sid + "/reset", {})
st, r = call("POST", "/chat", {"session_id": sid, "message": "转 200"})
texts = " ".join(b.get("text", "") for b in r["blocks"] if b["type"] == "text")
check("G5 会话 reset 清槽位（不沿用遗留收款人直接产卡）",
      r.get("pending_confirmation") is None and "转给谁" in texts,
      f"pending={r.get('pending_confirmation') is not None}")

n_pass = sum(1 for _, ok, _ in results if ok)
print(f"\n合计 {n_pass}/{len(results)} 通过")
sys.exit(0 if n_pass == len(results) else 1)
