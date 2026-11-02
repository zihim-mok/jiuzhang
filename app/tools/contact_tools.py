"""F3 联系人检索（T10）——备注仅作检索与展示，永不产生授权（N13/NFR-03）。"""
from __future__ import annotations

from app.db.connection import get_conn
from app.tools.bill_tools import DEMO_USER
from app.tools.registry import l0_tool, result


def _mask_phone(phone: str | None) -> str:
    if not phone:
        return ""
    return phone if "*" in phone else (phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else phone)


@l0_tool("contacts.search")
def contacts_search(query: str) -> dict:
    q = (query or "").strip()
    if not q:
        raise ValueError("query 不能为空")
    return _search(q)


def _search(q: str) -> dict:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT contact_id, name, phone, remark FROM contacts WHERE user_id=?"
            " ORDER BY name", (DEMO_USER,)).fetchall()
    finally:
        conn.close()
    items = []
    for r in rows:
        phone = r["phone"] or ""
        if (q in r["name"]) or (q in phone) or (r["remark"] and q in r["remark"]):
            items.append({
                "contact_id": r["contact_id"],
                "name": r["name"],
                "phone_masked": _mask_phone(phone),
                "remark": r["remark"] or "",   # 不可信数据，前端纯文本渲染
            })
    items.sort(key=lambda x: (0 if q in x["name"] else 1, x["name"]))
    return result("contacts.search", {"query": q}, {"items": items, "total": len(items)})


def contacts_list_all() -> dict:
    """全量联系人（规划层做姓名/尾号匹配用；L0）。"""
    return _list_all()


def _list_all() -> dict:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT contact_id, name, phone, remark FROM contacts WHERE user_id=?"
            " ORDER BY name", (DEMO_USER,)).fetchall()
    finally:
        conn.close()
    items = [{"contact_id": r["contact_id"], "name": r["name"],
              "phone_masked": _mask_phone(r["phone"] or ""),
              "remark": r["remark"] or ""} for r in rows]
    return result("contacts.search", {"query": "*"}, {"items": items, "total": len(items)})
