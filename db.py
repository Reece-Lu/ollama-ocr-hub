"""SQLite 层：密钥管理 + 请求记账。

代理进程和面板进程共用这一个数据库文件，靠 WAL 模式并发读写，
不需要额外的进程间通信。
"""

import ipaddress
import os
import secrets
import sqlite3
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("OCR_HUB_DB", os.path.join(BASE_DIR, "data", "hub.db"))

KEY_PREFIX = "sk-lan-"

SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
  id         INTEGER PRIMARY KEY,
  name       TEXT UNIQUE NOT NULL,
  api_key    TEXT UNIQUE NOT NULL,
  created_at REAL NOT NULL,
  revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS requests (
  id          INTEGER PRIMARY KEY,
  name        TEXT,
  client_ip   TEXT,
  model       TEXT,
  image_count INTEGER DEFAULT 0,
  image_bytes INTEGER DEFAULT 0,
  status      TEXT NOT NULL,
  enqueued_at REAL,
  started_at  REAL,
  finished_at REAL,
  queue_ms    INTEGER,
  process_ms  INTEGER,
  out_chars   INTEGER,
  error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_req_enqueued ON requests(enqueued_at);
CREATE INDEX IF NOT EXISTS idx_req_status   ON requests(status);
CREATE INDEX IF NOT EXISTS idx_req_name     ON requests(name);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS 不会给已有数据库补列，所以在这里做轻量迁移。
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(requests)")
        }
        if "client_ip" not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN client_ip TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_req_client_ip ON requests(client_ip)"
        )


def reset_stale() -> int:
    """代理重启时调用，清掉上次崩溃残留的僵尸行。"""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE requests SET status='aborted', finished_at=? "
            "WHERE status IN ('queued','running')",
            (time.time(),),
        )
        return cur.rowcount


def local_day_start() -> float:
    now = datetime.now()
    return datetime(now.year, now.month, now.day).timestamp()


# --- 密钥 ---------------------------------------------------------------


def issue_key(name: str):
    """按登记标识幂等发放。同一个标识重复领取，永远拿到同一把 key。

    返回 (api_key, is_new)。
    """
    name = name.strip()
    if not name:
        raise ValueError("登记标识不能为空")

    with connect() as conn:
        row = conn.execute("SELECT * FROM keys WHERE name=?", (name,)).fetchone()
        if row is not None:
            if row["revoked"]:
                # 被吊销过的，重新生成一把
                new_key = KEY_PREFIX + secrets.token_urlsafe(18)
                conn.execute(
                    "UPDATE keys SET api_key=?, revoked=0, created_at=? WHERE name=?",
                    (new_key, time.time(), name),
                )
                return new_key, True
            return row["api_key"], False

        new_key = KEY_PREFIX + secrets.token_urlsafe(18)
        conn.execute(
            "INSERT INTO keys (name, api_key, created_at, revoked) VALUES (?,?,?,0)",
            (name, new_key, time.time()),
        )
        return new_key, True


def issue_key_for_ip(client_ip: str):
    """按客户端 IP 发放密钥，并把 IPv4-mapped IPv6 统一成 IPv4。"""
    value = (client_ip or "").strip().split("%", 1)[0]
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("未能读取有效的本机 IP") from exc

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return issue_key(str(parsed))


def lookup_key(api_key: str):
    if not api_key:
        return None
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM keys WHERE api_key=? AND revoked=0", (api_key,)
        ).fetchone()


def list_keys():
    with connect() as conn:
        return conn.execute(
            "SELECT name, api_key, created_at, revoked FROM keys ORDER BY created_at"
        ).fetchall()


def revoke_key(name: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE keys SET revoked=1 WHERE name=?", (name.strip(),))


# --- 请求生命周期 -------------------------------------------------------


def log_enqueue(
    name: str,
    model: str,
    image_count: int,
    image_bytes: int,
    client_ip: str = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO requests "
            "(name, client_ip, model, image_count, image_bytes, status, enqueued_at) "
            "VALUES (?,?,?,?,?,'queued',?)",
            (name, client_ip, model, image_count, image_bytes, time.time()),
        )
        return cur.lastrowid


def log_start(rid: int) -> None:
    now = time.time()
    with connect() as conn:
        conn.execute(
            "UPDATE requests SET status='running', started_at=?, "
            "queue_ms=CAST((? - enqueued_at) * 1000 AS INTEGER) WHERE id=?",
            (now, now, rid),
        )


def log_finish(rid: int, status: str, out_chars: int = 0, error: str = None) -> None:
    now = time.time()
    with connect() as conn:
        conn.execute(
            "UPDATE requests SET status=?, finished_at=?, out_chars=?, error=?, "
            "process_ms=CAST((? - COALESCE(started_at, enqueued_at)) * 1000 AS INTEGER) "
            "WHERE id=?",
            (status, now, out_chars, error, now, rid),
        )


# --- 面板查询 -----------------------------------------------------------


def stats_live() -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT "
            "SUM(status='queued')  AS queued, "
            "SUM(status='running') AS running "
            "FROM requests WHERE enqueued_at > ?",
            (time.time() - 86400,),
        ).fetchone()
    return {"queued": row["queued"] or 0, "running": row["running"] or 0}


def stats_today() -> dict:
    day0 = local_day_start()
    with connect() as conn:
        row = conn.execute(
            "SELECT "
            "COUNT(*)                                   AS total, "
            "SUM(status='ok')                           AS ok, "
            "SUM(status='error')                        AS err, "
            "COALESCE(SUM(image_count), 0)              AS images, "
            "AVG(CASE WHEN status='ok' THEN process_ms END) AS avg_ms "
            "FROM requests WHERE enqueued_at >= ?",
            (day0,),
        ).fetchone()
    return {
        "total": row["total"] or 0,
        "ok": row["ok"] or 0,
        "err": row["err"] or 0,
        "images": row["images"] or 0,
        "avg_ms": row["avg_ms"],
    }


def recent(limit: int = 100, name: str = None):
    sql = (
        "SELECT id, name, client_ip, model, image_count, status, enqueued_at, "
        "queue_ms, process_ms, out_chars, error FROM requests"
    )
    params = []
    if name:
        sql += " WHERE name=?"
        params.append(name)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def stats_by_client(limit: int = 50):
    """返回今天按来源 IP 聚合的用量。旧记录没有 IP 时统一显示为“未知”。"""
    with connect() as conn:
        return conn.execute(
            "SELECT COALESCE(NULLIF(client_ip, ''), '未知') AS client_ip, "
            "COUNT(*) AS requests, "
            "COALESCE(SUM(image_count), 0) AS images, "
            "COALESCE(SUM(status='ok'), 0) AS ok, "
            "COALESCE(SUM(status='error'), 0) AS errors, "
            "AVG(CASE WHEN status='ok' THEN process_ms END) AS avg_ms, "
            "MAX(enqueued_at) AS last_seen "
            "FROM requests WHERE enqueued_at >= ? "
            "GROUP BY COALESCE(NULLIF(client_ip, ''), '未知') "
            "ORDER BY requests DESC, last_seen DESC LIMIT ?",
            (local_day_start(), limit),
        ).fetchall()


def purge_before(days: int) -> int:
    cutoff = time.time() - days * 86400
    with connect() as conn:
        cur = conn.execute("DELETE FROM requests WHERE enqueued_at < ?", (cutoff,))
        return cur.rowcount


if __name__ == "__main__":
    init_db()
    print(f"已初始化数据库: {DB_PATH}")
