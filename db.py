# -*- coding: utf-8 -*-
"""持久化缓存层：MySQL 优先（db_config.json 配好即用），连不上自动落 SQLite。

表结构（两种后端一致）：
  cache(k TEXT PK, payload TEXT, expires_at INT, fetched_at INT)

k 形如 "train|2026-10-21|BJP|SHH" / "transfer|..."；payload 为 JSON 字符串。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "db_config.json"
SQLITE_FILE = ROOT / "cache.db"


def _load_mysql_config() -> dict | None:
    if not CONFIG_FILE.exists():
        return None
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("mysql")
        if cfg and cfg.get("user"):
            return cfg
    except (ValueError, OSError):
        pass
    return None


class Cache:
    """线程安全的 KV 缓存，get 时过滤过期项。backend ∈ {mysql, sqlite}。"""

    def __init__(self, backend: str, mysql_cfg: dict | None = None,
                 sqlite_path: Path | None = None):
        self.backend = backend
        self._mysql_cfg = mysql_cfg
        self._lock = threading.Lock()
        if backend == "sqlite":
            self._sqlite_path = str(sqlite_path or SQLITE_FILE)
            with self._sqlite() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS cache ("
                    "k TEXT PRIMARY KEY, payload TEXT, "
                    "expires_at INTEGER, fetched_at INTEGER)")
        else:
            db_name = self._mysql_cfg.get("database")
            if db_name:
                if not re.fullmatch(r"\w{1,64}", db_name):
                    raise ValueError(f"非法数据库名: {db_name!r}")
                with self._mysql_connect(with_db=False) as conn, conn.cursor() as cur:
                    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                                "DEFAULT CHARSET utf8mb4")
            with self._mysql() as conn, conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS cache ("
                    "k VARCHAR(191) PRIMARY KEY, payload MEDIUMTEXT, "
                    "expires_at BIGINT, fetched_at BIGINT) "
                    "DEFAULT CHARSET=utf8mb4")

    # ---- 连接管理 ----
    def _sqlite(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._sqlite_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _mysql_connect(self, with_db: bool = True):
        import pymysql  # 延迟导入：SQLite 模式不强依赖
        cfg = dict(self._mysql_cfg)
        cfg.setdefault("host", "127.0.0.1")
        cfg.setdefault("port", 3306)
        cfg.setdefault("charset", "utf8mb4")
        cfg.setdefault("autocommit", True)
        if not with_db:
            cfg.pop("database", None)
        return pymysql.connect(**cfg)

    def _mysql(self):
        return self._mysql_connect(with_db=True)

    # ---- KV ----
    def get(self, key: str) -> dict | None:
        now = int(time.time())
        with self._lock:
            try:
                if self.backend == "mysql":
                    with self._mysql() as conn, conn.cursor() as cur:
                        cur.execute("SELECT payload, expires_at FROM cache WHERE k=%s", (key,))
                        row = cur.fetchone()
                else:
                    with self._sqlite() as conn:
                        row = conn.execute(
                            "SELECT payload, expires_at FROM cache WHERE k=?", (key,)).fetchone()
            except Exception:
                return None  # 缓存层故障不拖垮主流程，回退到实时查询
        if not row or row[1] < now:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def put(self, key: str, payload: dict, ttl_sec: int):
        now = int(time.time())
        blob = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            try:
                if self.backend == "mysql":
                    with self._mysql() as conn, conn.cursor() as cur:
                        cur.execute(
                            "REPLACE INTO cache (k, payload, expires_at, fetched_at) "
                            "VALUES (%s,%s,%s,%s)", (key, blob, now + ttl_sec, now))
                else:
                    with self._sqlite() as conn:
                        conn.execute(
                            "REPLACE INTO cache (k, payload, expires_at, fetched_at) "
                            "VALUES (?,?,?,?)", (key, blob, now + ttl_sec, now))
            except Exception:
                pass  # 写失败仅损失一次缓存命中

    def purge_expired(self) -> int:
        now = int(time.time())
        with self._lock:
            try:
                if self.backend == "mysql":
                    with self._mysql() as conn, conn.cursor() as cur:
                        cur.execute("DELETE FROM cache WHERE expires_at<%s", (now,))
                        return cur.rowcount
                with self._sqlite() as conn:
                    cur = conn.execute("DELETE FROM cache WHERE expires_at<?", (now,))
                    return cur.rowcount
            except Exception:
                return 0

    def stats(self) -> dict:
        with self._lock:
            try:
                if self.backend == "mysql":
                    with self._mysql() as conn, conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*) FROM cache")
                        n = cur.fetchone()[0]
                else:
                    with self._sqlite() as conn:
                        n = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            except Exception:
                n = -1
        return {"backend": self.backend, "entries": n}


def create_cache() -> Cache:
    """MySQL 配置存在且可连 → MySQL；否则 SQLite 兜底（功能等价，单机够用）。"""
    cfg = _load_mysql_config()
    if cfg:
        try:
            c = Cache("mysql", mysql_cfg=cfg)
            print(f"[cache] MySQL {cfg.get('database','(默认库)')} 已连接")
            return c
        except Exception as e:
            print(f"[cache] MySQL 连接失败({e})，落 SQLite")
    c = Cache("sqlite")
    print(f"[cache] SQLite {SQLITE_FILE.name}")
    return c
