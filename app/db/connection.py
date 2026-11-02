"""SQLite 连接管理。

纪律：金额一律整数最小货币单位（分）；参数绑定防注入；启用外键约束。

这里原来每次开连接都跑一遍 `PRAGMA journal_mode = WAL`。
WAL 模式的切换需要抢数据库的独占锁，并发时抢不到会抛 **`attempt to write a readonly
database`**（不是常见的 `database is locked`，所以极易被误判成文件权限问题）。
实测：两个线程同时 approve 同一张确认单，30 轮里有 2 轮报这个错，直接漏成 HTTP 500。

改法：
- `journal_mode` 是**持久化在库文件里**的属性，只需在建库时设一次（见 init_db），
  后续连接自动是 WAL，不必也不该每个连接重设；
- 连接改用 `timeout` + `busy_timeout`，并发写按「等一会儿」处理，而不是立刻失败。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core.config import get_config

# 并发写等待上限（秒）。演示场景单人操作，5 秒足够覆盖任何正常的写冲突；
# 真接多用户要配合队列/单写者模型，不能靠拉长超时。
BUSY_TIMEOUT_S = 5.0


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    """获取 SQLite 连接（每次新建，调用方负责 close）。"""
    path = db_path or get_config().database.resolved_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S)
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn
