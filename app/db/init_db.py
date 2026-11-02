"""建库与重置脚本。

用法：
  python -m app.db.init_db            # 首次建库（库已存在时报错，防止意外覆盖）
  python -m app.db.init_db --reset    # 显式重置：删除并重新生成模拟数据
  python -m app.db.init_db --verify   # 打印数据完整性核对结果

纪律：数据初始化只在本脚本显式执行；应用运行或失败重试时绝不重新生成账单。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.core.config import get_config
from app.db.connection import get_conn
from app.db.seed import DATA_END, DATA_START, SEED, SIM_TODAY, content_checksum, generate

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def init(reset: bool = False) -> None:
    db_path = get_config().database.resolved_path
    if db_path.exists() and not reset:
        print(f"[init_db] 库已存在：{db_path}\n"
              f"[init_db] 如需重新生成请显式运行: python -m app.db.init_db --reset")
        sys.exit(1)
    if reset:
        for suffix in ["", "-wal", "-shm"]:
            p = db_path.with_name(db_path.name + suffix)
            if p.exists():
                p.unlink()
        print(f"[init_db] 已删除旧库，开始重建：{db_path}")

    conn = get_conn()
    try:
        # journal_mode 是库文件的持久属性，建库时设一次即可，后续连接自动继承。
        # 放在这里而不是每次 get_conn()：并发下重设 WAL 要抢独占锁，抢不到会抛
        # `attempt to write a readonly database`。
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        generate(conn)
        conn.commit()
        n_txn = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        n_sub = conn.execute("SELECT COUNT(*) FROM subscription_authorizations").fetchone()[0]
        print(f"[init_db] 完成：交易 {n_txn} 笔，订阅授权 {n_sub} 条，"
              f"期间 {DATA_START} ~ {DATA_END}，种子 SEED={SEED}，模拟当前日 {SIM_TODAY}")
        print(f"[init_db] 内容校验和: {content_checksum(conn)}")
    finally:
        conn.close()


def verify() -> None:
    conn = get_conn()
    try:
        ok = True
        # 1) 订阅累计扣款与交易对账一致（N25 验收点）
        rows = conn.execute("""
            SELECT s.sub_id, s.counterparty, s.total_deducted_cents,
                   COALESCE(SUM(t.amount_cents), 0) AS txn_sum
            FROM subscription_authorizations s
            LEFT JOIN transactions t
              ON t.counterparty = s.counterparty AND t.txn_type = 'subscription'
            GROUP BY s.sub_id
        """).fetchall()
        for r in rows:
            match = "OK" if r["total_deducted_cents"] == r["txn_sum"] else "MISMATCH"
            if match != "OK":
                ok = False
            print(f"  对账 {r['sub_id']}: 表内 {r['total_deducted_cents']} vs 交易 {r['txn_sum']} [{match}]")
        # 2) 闲置样例：云存储无近期使用记录
        last = conn.execute(
            "SELECT MAX(used_at) AS last FROM usage_records WHERE sub_id='sub-cloud'").fetchone()["last"]
        print(f"  闲置样例 sub-cloud 最后使用: {last}（应早于 2026-09-30）")
        # 3) 异常样例存在（P4 组 = 002-006 五笔便利购）
        for tid in ["tx-anomaly-001", "tx-anomaly-002", "tx-anomaly-003",
                    "tx-anomaly-004", "tx-anomaly-005", "tx-anomaly-006",
                    "tx-malremark-001"]:
            cnt = conn.execute("SELECT COUNT(*) FROM transactions WHERE txn_id=?", (tid,)).fetchone()[0]
            print(f"  样例 {tid}: {'存在' if cnt else '缺失'}")
            ok = ok and bool(cnt)
        print(f"[verify] {'全部通过' if ok else '存在不一致，请检查'}")
        sys.exit(0 if ok else 1)
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="删除旧库并重新生成")
    ap.add_argument("--verify", action="store_true", help="数据完整性核对")
    args = ap.parse_args()
    if args.verify:
        verify()
    else:
        init(reset=args.reset)
