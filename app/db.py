"""SQLite 存取层：连接管理、建表、事务。

- 数据库文件路径由环境变量 DB_PATH 指定（默认 ./data/app.db），实现重启后可查。
- 写操作一律走 immediate_tx()：BEGIN IMMEDIATE 先取写锁，把并发批处理串行化，
  其结果与"按生效顺序逐批处理"一致；事务内任何异常都会整体回滚。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    round_id    TEXT PRIMARY KEY,
    roster_hash TEXT NOT NULL,          -- 清册内容摘要，用于幂等登记
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    round_id   TEXT NOT NULL REFERENCES rounds(round_id),
    topic_code TEXT NOT NULL,
    quota      INTEGER NOT NULL,        -- 名额
    position   INTEGER NOT NULL,        -- 登记顺序（补样时的确定处理次序）
    PRIMARY KEY (round_id, topic_code)
);

CREATE TABLE IF NOT EXISTS samples (
    round_id   TEXT NOT NULL,
    topic_code TEXT NOT NULL,
    invoice_no TEXT NOT NULL,
    active     INTEGER NOT NULL,        -- 1=现行有效样本, 0=已冲红退出
    source     TEXT NOT NULL,           -- 'roster' 或补入它的批号
    position   INTEGER NOT NULL,
    PRIMARY KEY (round_id, topic_code, invoice_no)
);

CREATE TABLE IF NOT EXISTS candidates (
    round_id   TEXT NOT NULL,
    topic_code TEXT NOT NULL,
    invoice_no TEXT NOT NULL,
    available  INTEGER NOT NULL,        -- 1=可用备选, 0=已占用/已冲红
    position   INTEGER NOT NULL,
    PRIMARY KEY (round_id, topic_code, invoice_no)
);

CREATE TABLE IF NOT EXISTS batches (
    round_id     TEXT NOT NULL,
    batch_no     TEXT NOT NULL,
    seq          INTEGER NOT NULL,      -- 生效顺序（提交并发时的串行次序）
    invoice_key  TEXT NOT NULL,         -- 规范化票号集合（排序去重拼接），用于幂等判定
    receipt_json TEXT NOT NULL,         -- 回执原文，重送同组票号时原样返回
    created_at   TEXT NOT NULL,
    PRIMARY KEY (round_id, batch_no)
);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    path = db_path()
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        if db_path() != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def immediate_tx():
    """写事务：先取写锁串行化并发写；成功提交，失败整批回滚。"""
    conn = connect()
    began = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        began = True
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if began:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()


@contextmanager
def read_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
