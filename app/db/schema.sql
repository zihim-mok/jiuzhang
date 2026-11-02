-- SQLite Schema v0.1
-- 纪律：金额一律 INTEGER 最小货币单位（分）；禁浮点；外键约束开启。
-- 订阅授权表按8 字段蓝图建表，另含刚柔分类字段。

CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    phone        TEXT NOT NULL,
    persona_note TEXT,                -- 模拟画像说明
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id    TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(user_id),
    account_name  TEXT NOT NULL,
    account_type  TEXT NOT NULL,
    balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0),
    currency      TEXT NOT NULL DEFAULT 'CNY'
);

-- 收款联系人。remark 为不可信业务数据：仅用于检索展示，
-- 不拼入系统指令、不产生任何授权。
CREATE TABLE IF NOT EXISTS contacts (
    contact_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(user_id),
    name       TEXT NOT NULL,
    phone      TEXT,
    remark     TEXT                   -- 不可信数据
);
CREATE INDEX IF NOT EXISTS idx_contacts_user_name ON contacts(user_id, name);

-- 交易流水。category 允许 '未知'（F1 要求未知类别显式展示）。
CREATE TABLE IF NOT EXISTS transactions (
    txn_id             TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL REFERENCES users(user_id),
    account_id         TEXT NOT NULL REFERENCES accounts(account_id),
    txn_time           TEXT NOT NULL,          -- ISO 8601
    direction          TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    amount_cents       INTEGER NOT NULL CHECK (amount_cents > 0),
    counterparty       TEXT NOT NULL,
    merchant_normalized TEXT,                  -- 商户归一化名（不可信数据）
    category           TEXT NOT NULL DEFAULT '未知',
    txn_type           TEXT NOT NULL,          -- salary/expense/subscription/transfer/anomaly_sample 等
    remark             TEXT,                   -- 不可信数据
    channel            TEXT
);
CREATE INDEX IF NOT EXISTS idx_txn_user_time ON transactions(user_id, txn_time);
CREATE INDEX IF NOT EXISTS idx_txn_merchant  ON transactions(user_id, merchant_normalized);

-- 订阅授权表（8 字段：对方主体/授权内容/签约时间/有效期/退出方式/
-- 当前状态/累计扣款/最近一次活动；另含刚柔分类与周期字段）。
CREATE TABLE IF NOT EXISTS subscription_authorizations (
    sub_id               TEXT PRIMARY KEY,
    user_id              TEXT NOT NULL REFERENCES users(user_id),
    counterparty         TEXT NOT NULL,      -- ① 对方主体
    auth_scope           TEXT NOT NULL,      -- ② 授权内容（现实不可得字段，模拟值）
    signed_at            TEXT NOT NULL,      -- ③ 签约时间
    valid_until          TEXT NOT NULL,      -- ④ 有效期
    exit_method          TEXT NOT NULL,      -- ⑤ 退出方式（绑定 L1 取消流程）
    status               TEXT NOT NULL,      -- ⑥ 当前状态 active/cancelled
    total_deducted_cents INTEGER NOT NULL DEFAULT 0 CHECK (total_deducted_cents >= 0),  -- ⑦ 累计扣款（须与交易对账一致）
    last_activity_at     TEXT,               -- ⑧ 最近一次活动（来自 usage_records，闲置判断唯一依据）
    -- 刚柔分类：flexible(柔性-帮你退出) / rigid(刚性-帮你保住)
    rigidity             TEXT NOT NULL CHECK (rigidity IN ('flexible', 'rigid')),
    rigidity_basis       TEXT NOT NULL,      -- 分类依据（可回溯：商户类别+规则）
    cycle                TEXT NOT NULL DEFAULT 'monthly',
    charge_day           INTEGER NOT NULL CHECK (charge_day BETWEEN 1 AND 28),
    monthly_amount_cents INTEGER NOT NULL CHECK (monthly_amount_cents > 0)
);

-- 模拟使用记录：闲置判断的唯一依据（不从扣费本身推出闲置）。
CREATE TABLE IF NOT EXISTS usage_records (
    usage_id  TEXT PRIMARY KEY,
    user_id   TEXT NOT NULL REFERENCES users(user_id),
    sub_id    TEXT NOT NULL REFERENCES subscription_authorizations(sub_id),
    used_at   TEXT NOT NULL,
    activity  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_sub ON usage_records(sub_id, used_at);

CREATE TABLE IF NOT EXISTS reminders (
    reminder_id     TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    sub_id          TEXT NOT NULL REFERENCES subscription_authorizations(sub_id),
    remind_for_date TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'notified', 'dismissed')),
    created_at      TEXT NOT NULL
);

-- 定时转账任务：基于已确认参数执行，触发时不重新生成付款参数。
CREATE TABLE IF NOT EXISTS scheduled_transfers (
    task_id            TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL REFERENCES users(user_id),
    account_id         TEXT NOT NULL REFERENCES accounts(account_id),
    contact_id         TEXT NOT NULL REFERENCES contacts(contact_id),
    amount_cents       INTEGER NOT NULL CHECK (amount_cents > 0),
    recurrence         TEXT NOT NULL CHECK (recurrence IN ('once', 'monthly')),
    day_of_month       INTEGER CHECK (day_of_month IS NULL OR day_of_month BETWEEN 1 AND 28),
    next_run_date      TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'executing', 'executed', 'cancelled', 'failed', 'needs_review')),
    authorized_params_json TEXT NOT NULL,   -- 已确认参数快照
    created_via_confirmation_id TEXT,
    executed_txn_id    TEXT,
    created_at         TEXT NOT NULL
);

-- 确认记录：确认绑定用户+操作+规范化参数+状态，参数变即失效。
CREATE TABLE IF NOT EXISTS confirmations (
    confirmation_id        TEXT PRIMARY KEY,
    user_id                TEXT NOT NULL REFERENCES users(user_id),
    action                 TEXT NOT NULL,
    level                  TEXT NOT NULL CHECK (level IN ('L1', 'L2')),
    normalized_params_json TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'pending'
                           -- executing：已被某个请求「抢占」，正在执行工具。抢占是原子的
                           --   （UPDATE ... WHERE status='pending' 看 rowcount），
                           --   否则两个并发 approve 会各读一次 pending 再各扣一次钱。
                           -- failed：工具执行报错。此时资金状态不明，不能退回 pending
                           --   让用户再点（B13 精神：先核查再重试）。
                           CHECK (status IN ('pending', 'executing', 'approved',
                                             'rejected', 'expired', 'failed')),
    created_at             TEXT NOT NULL,
    expires_at             TEXT NOT NULL,
    decided_at             TEXT,
    result_ref             TEXT            -- 指向执行凭证/任务/交易
);

-- 幂等键：重复请求/确认/重试不得重复扣款。
CREATE TABLE IF NOT EXISTS idempotency_keys (
    request_id    TEXT PRIMARY KEY,
    action        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'processing' CHECK (status IN ('processing', 'done', 'failed')),
    result_txn_id TEXT,
    created_at    TEXT NOT NULL
);

-- 审计留痕：计划、确认、工具调用、执行状态；密钥不入日志。
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    request_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

-- 会话记忆（M1）：归属边界 = session_id。单用户演示画像，不加 user_id（见记忆模块设计 v0.1 第四节）。
-- 纪律：本表只用于「理解指代」与「回读/导出」；金额、余额、日期一律回工具取，不得采信本表数字。
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content    TEXT NOT NULL,          -- 原文，仅用于展示与导出
    intent_id  TEXT,                   -- assistant 行填，供规则摘要
    tool_names TEXT,                   -- JSON 数组，如 ["bill.summary"]
    -- 时基提醒：本列是 `datetime('now')`，即 **UTC**、
    -- 格式 'YYYY-MM-DD HH:MM:SS'；而 confirmations 等表是 now8() 的 +08:00 ISO 串。
    -- 两者不参与任何跨表时间比较，TTL 用同一时基自洽，所以功能上无碍；
    -- 但**前端若要展示这一列，必须 +8 小时**，否则会少 8 小时。
    -- 没有直接改成 +08:00 的原因：改格式会让存量行的字符串比较全部失效，
    -- 等于把现有记忆静默丢光（记忆是 24h 过期的旁路数据，不值得为一致性冒这个险）。
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, id);
