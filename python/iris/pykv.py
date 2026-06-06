import json
import sqlite3
import threading
import time


class PyKV:
    _instance = None
    _lock = threading.Lock()
    _local = threading.local()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.filename = "iris.db"
            self._initialized = True

    def _get_db(self) -> sqlite3.Connection:
        if not hasattr(self._local, "db") or self._local.db is None:
            if self.filename is None:
                raise RuntimeError("Database filename not set.")
            db = sqlite3.connect(self.filename, check_same_thread=False)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("CREATE TABLE IF NOT EXISTS kv_pairs (key TEXT PRIMARY KEY, value TEXT)")
            db.commit()
            self._local.db = db
        return self._local.db

    def open(self, filename: str):
        if self.filename is None:
            self.filename = filename

    def close(self):
        if hasattr(self._local, "db") and self._local.db is not None:
            self._local.db.close()
            self._local.db = None

    def get(self, key: str):
        row = self._get_db().execute(
            "SELECT value FROM kv_pairs WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return False
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return False

    def get_kv(self, key: str):
        value = self.get(key)
        return {"key": key, "value": value} if value is not False else False

    def put(self, key: str, value):
        db = self._get_db()
        for attempt in range(3):
            try:
                db.execute(
                    "INSERT OR REPLACE INTO kv_pairs (key, value) VALUES (?, ?)",
                    (key, json.dumps(value)),
                )
                db.commit()
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    raise

    def delete(self, key: str):
        db = self._get_db()
        for attempt in range(3):
            try:
                db.execute("DELETE FROM kv_pairs WHERE key = ?", (key,))
                db.commit()
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    raise

    def search(self, search_string: str) -> list[dict]:
        rows = self._get_db().execute(
            "SELECT key, value FROM kv_pairs WHERE value LIKE ?",
            (f"%{search_string}%",),
        ).fetchall()
        return [{"key": k, "value": json.loads(v)} for k, v in rows if _valid_json(v)]

    def search_key(self, search_string: str) -> list[dict]:
        rows = self._get_db().execute(
            "SELECT key, value FROM kv_pairs WHERE key LIKE ?",
            (f"%{search_string}%",),
        ).fetchall()
        return [{"key": k, "value": json.loads(v)} for k, v in rows if _valid_json(v)]

    def search_json(self, value_key: str, search_string: str) -> list[dict]:
        """JSON 필드 값으로 검색. value_key는 점(.) 구분 경로 지원 (예: 'user.name')."""
        json_path = "$." + value_key
        try:
            rows = self._get_db().execute(
                "SELECT key, value FROM kv_pairs WHERE CAST(json_extract(value, ?) AS TEXT) LIKE ?",
                (json_path, f"%{search_string}%"),
            ).fetchall()
            return [{"key": k, "value": json.loads(v)} for k, v in rows]
        except sqlite3.OperationalError:
            return self._search_json_fallback(value_key, search_string)

    def _search_json_fallback(self, value_key: str, search_string: str) -> list[dict]:
        results = []
        for key, value_str in self._get_db().execute("SELECT key, value FROM kv_pairs").fetchall():
            try:
                value = json.loads(value_str)
                node = value
                for part in value_key.split("."):
                    node = node[part] if isinstance(node, dict) and part in node else None
                    if node is None:
                        break
                if node is not None and search_string in str(node):
                    results.append({"key": key, "value": value})
            except (json.JSONDecodeError, TypeError):
                pass
        return results

    def list_keys(self) -> list[str]:
        return [
            row[0]
            for row in self._get_db().execute("SELECT key FROM kv_pairs").fetchall()
        ]


def _valid_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except (json.JSONDecodeError, TypeError):
        return False
