# -*- coding: utf-8 -*-
"""跨进程共享 AI 作答结果。

同一节课的多个账号进程共用项目目录下的 SQLite 文件：
- 先查已经算好的答案；
- 没有结果时，只有一个进程能抢到 computing 租约并调用 AI；
- 其他进程等待这个结果，不重复调用模型；
- 租约由后台线程定时续期，leader 进程退出后租约会过期，下一个进程可接管。
"""

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "answer_cache.sqlite3")

LEASE_SECONDS = 20
MAX_ATTEMPTS = 3
TTL_SECONDS = 24 * 3600


_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"<img[^>]*src\s*=\s*[\"']?([^\"'\s>]+)", re.I)


def _plain(value):
    """去掉 HTML 标签和多余空白，避免不同账号拿到的样式/签名参数不一样。"""
    if value is None:
        return ""
    text = _TAG_RE.sub(" ", str(value))
    return re.sub(r"\s+", " ", text).strip()


def _image_signature(problem):
    """只取图片地址的 path，忽略签名 query（query 里常带账号/时间，不能参与 key）。"""
    urls = []
    texts = [problem.get("body")]
    for option in problem.get("options") or []:
        if isinstance(option, dict):
            texts.append(option.get("value") or option.get("text")
                         or option.get("content"))
        else:
            texts.append(option)
    for text in texts:
        if not text:
            continue
        for match in _IMG_RE.finditer(str(text)):
            urls.append(match.group(1).strip())
    for field in ("pics", "images", "image", "pic"):
        value = problem.get(field)
        for item in (value if isinstance(value, list) else [value]):
            if isinstance(item, dict):
                item = (item.get("url") or item.get("src")
                        or item.get("pic") or item.get("value"))
            if isinstance(item, str) and item.strip():
                urls.append(item.strip())
    paths = []
    for url in urls:
        try:
            paths.append(urlparse(url).path or url.split("?")[0])
        except Exception:
            paths.append(url.split("?")[0])
    return sorted(set(paths))


def problem_signature(problem):
    """把题目内容压成稳定指纹，防止 problemId 相同但题目内容变了还复用。"""
    if not isinstance(problem, dict):
        return ""
    data = {
        "problemType": problem.get("problemType") or 0,
        "body": _plain(problem.get("body")),
        "options": [],
        "images": _image_signature(problem),
        "blanks": len(problem.get("blanks") or []),
    }
    for option in problem.get("options") or []:
        if isinstance(option, dict):
            data["options"].append([
                str(option.get("key") or ""),
                _plain(option.get("value") or option.get("text")
                       or option.get("content")),
            ])
        else:
            data["options"].append(["", _plain(option)])
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Lease:
    """一次 leader 租约：后台续期，发布结果或失败后停止。"""

    def __init__(self, cache, key, owner_token, lease_seconds):
        self.cache = cache
        self.key = key
        self.owner_token = owner_token
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()

    def _heartbeat(self):
        interval = max(1.0, self.lease_seconds / 3.0)
        while not self._stop.wait(interval):
            try:
                ok = self.cache.renew(self.key, self.owner_token,
                                      self.lease_seconds)
            except Exception as e:
                # SQLite 短暂锁冲突不要直接放弃续租；下一次心跳再试
                print(f"[AI共享] 续租失败：{type(e).__name__}: {e}")
                continue
            if not ok:
                self._stop.set()
                break

    def publish(self, result, source, problem_type):
        try:
            return self.cache.publish(self.key, self.owner_token, result,
                                      source, problem_type)
        finally:
            self.stop()

    def fail(self, error, retry_after=1.0):
        try:
            self.cache.fail(self.key, self.owner_token, error, retry_after)
        finally:
            self.stop()

    def stop(self):
        self._stop.set()
        if (self._thread.is_alive()
                and threading.current_thread() is not self._thread):
            self._thread.join(timeout=1.0)


class AnswerShareCache:
    def __init__(self, path=None, lease_seconds=LEASE_SECONDS,
                 max_attempts=MAX_ATTEMPTS, ttl_seconds=TTL_SECONDS):
        self.path = path or DEFAULT_DB
        self.lease_seconds = float(lease_seconds)
        self.max_attempts = int(max_attempts)
        self.ttl_seconds = float(ttl_seconds)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS answer_share (
                    lesson_id TEXT NOT NULL,
                    problem_id TEXT NOT NULL,
                    body_hash TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    problem_type INTEGER,
                    result_json TEXT,
                    source TEXT,
                    status TEXT NOT NULL,
                    owner TEXT,
                    owner_token TEXT,
                    lease_until REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    retry_after REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (lesson_id, problem_id, body_hash, variant)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_answer_share_updated "
                         "ON answer_share(updated_at)")
            conn.execute("DELETE FROM answer_share WHERE updated_at < ?",
                         (time.time() - self.ttl_seconds,))
        finally:
            conn.close()

    def make_key(self, lesson_id, problem_id, problem, variant=""):
        return (str(lesson_id), str(problem_id),
                problem_signature(problem), str(variant))

    def _select(self, conn, key):
        return conn.execute(
            "SELECT * FROM answer_share WHERE lesson_id=? AND problem_id=? "
            "AND body_hash=? AND variant=?", key).fetchone()

    def _payload(self, row):
        try:
            result = json.loads(row["result_json"] or "null")
        except Exception:
            return None
        if not isinstance(result, (list, dict)) or not result:
            return None
        return {
            "result": result,
            "source": row["source"] or "AI 共享答案",
            "problem_type": row["problem_type"],
        }

    def acquire(self, key, owner):
        """返回 (status, payload)。

        status:
          hit     -> payload 是缓存结果
          leader  -> payload 是 Lease，只有调用方可以跑 AI
          wait    -> 其他进程正在算，调用方等待
          failed  -> 多次尝试都失败，不再重试
        """
        now = time.time()
        token = uuid.uuid4().hex
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._select(conn, key)
            if row is not None:
                if row["status"] == "done":
                    payload = self._payload(row)
                    conn.commit()
                    if payload is None:
                        return "failed", {"error": "缓存结果损坏"}
                    return "hit", payload
                if (row["status"] == "computing"
                        and row["lease_until"] and row["lease_until"] > now):
                    conn.commit()
                    return "wait", {"lease_until": row["lease_until"]}
                if (row["status"] == "failed"
                        and row["retry_after"] and row["retry_after"] > now):
                    conn.commit()
                    return "wait", {"retry_after": row["retry_after"]}
                attempts = int(row["attempts"] or 0) + 1
                if attempts > self.max_attempts:
                    conn.execute(
                        "UPDATE answer_share SET status='failed', "
                        "owner=NULL, owner_token=NULL, lease_until=NULL, "
                        "error=?, updated_at=? WHERE lesson_id=? AND "
                        "problem_id=? AND body_hash=? AND variant=?",
                        ("尝试次数超过上限", now) + key)
                    conn.commit()
                    return "failed", {"error": "尝试次数超过上限"}
                conn.execute(
                    "UPDATE answer_share SET status='computing', owner=?, "
                    "owner_token=?, lease_until=?, attempts=?, error=NULL, "
                    "retry_after=NULL, updated_at=? WHERE lesson_id=? AND "
                    "problem_id=? AND body_hash=? AND variant=?",
                    (owner, token, now + self.lease_seconds, attempts, now) + key)
                conn.commit()
                return "leader", Lease(self, key, token, self.lease_seconds)
            conn.execute(
                "INSERT INTO answer_share "
                "(lesson_id, problem_id, body_hash, variant, problem_type, "
                "status, owner, owner_token, lease_until, attempts, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, 'computing', ?, ?, ?, 1, ?, ?)",
                key + (0, owner, token, now + self.lease_seconds, now, now))
            conn.commit()
            return "leader", Lease(self, key, token, self.lease_seconds)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def wait(self, key, timeout, should_stop=None):
        """等 leader 的结果，返回 hit / retry / failed / timeout / stopped。"""
        end = time.time() + max(0.0, float(timeout))
        while True:
            if should_stop is not None and should_stop():
                return "stopped", None
            conn = self._connect()
            try:
                row = self._select(conn, key)
            finally:
                conn.close()
            now = time.time()
            if row is None:
                return "retry", None
            if row["status"] == "done":
                payload = self._payload(row)
                if payload is None:
                    return "failed", {"error": "缓存结果损坏"}
                return "hit", payload
            if row["status"] == "computing":
                if not row["lease_until"] or row["lease_until"] <= now:
                    return "retry", None
            elif row["status"] == "failed":
                if int(row["attempts"] or 0) >= self.max_attempts:
                    return "failed", {"error": row["error"] or "共享计算失败"}
                if not row["retry_after"] or row["retry_after"] <= now:
                    return "retry", None
            remaining = end - time.time()
            if remaining <= 0:
                return "timeout", None
            time.sleep(min(0.5, remaining))

    def renew(self, key, token, lease_seconds):
        now = time.time()
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE answer_share SET lease_until=?, updated_at=? "
                "WHERE lesson_id=? AND problem_id=? AND body_hash=? AND "
                "variant=? AND status='computing' AND owner_token=?",
                (now + lease_seconds, now) + key + (token,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def publish(self, key, token, result, source, problem_type):
        now = time.time()
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE answer_share SET status='done', result_json=?, "
                "source=?, problem_type=?, owner=NULL, owner_token=NULL, "
                "lease_until=NULL, error=NULL, retry_after=NULL, updated_at=? "
                "WHERE lesson_id=? AND problem_id=? AND body_hash=? AND "
                "variant=? AND status='computing' AND owner_token=?",
                (json.dumps(result, ensure_ascii=False), source, int(problem_type or 0),
                 now) + key + (token,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def fail(self, key, token, error, retry_after=1.0):
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE answer_share SET status='failed', error=?, "
                "retry_after=?, owner=NULL, owner_token=NULL, lease_until=NULL, "
                "updated_at=? WHERE lesson_id=? AND problem_id=? AND "
                "body_hash=? AND variant=? AND status='computing' AND "
                "owner_token=?",
                (str(error)[:500], now + max(0.0, float(retry_after)), now)
                + key + (token,))
            conn.commit()
        finally:
            conn.close()
