"""Local-only durable conversation snapshots for restoring browser chat history.

Only bounded, sanitized conversation data is stored. No model prompts, credentials,
raw attachment bytes, or uploaded file contents are accepted by this store.
"""
import json
import re
import time
from pathlib import Path

try:
    from .dbutil import Database
except ImportError:  # pragma: no cover
    from dbutil import Database

MAX_CONVERSATIONS = 100
MAX_MESSAGES = 1200
MAX_CONTENT = 100_000
MAX_TOTAL = 5 * 1024 * 1024
ROLES = {"user", "assistant", "tool"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,100}$")


class ConversationStore:
    def __init__(self, path):
        self.db = Database(Path(path), wal=True)
        with self.db.tx() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                data_json TEXT NOT NULL, updated_at REAL NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC)")

    @staticmethod
    def _sanitize(items):
        if not isinstance(items, list) or len(items) > MAX_CONVERSATIONS:
            raise ValueError("فهرست گفت‌وگوها معتبر یا بیش از حد بزرگ است.")
        cleaned, total = [], 0
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            if not isinstance(cid, str) or not ID_RE.fullmatch(cid) or cid in seen:
                continue
            seen.add(cid)
            title = item.get("title", "گفت‌وگوی جدید")
            if not isinstance(title, str):
                title = "گفت‌وگوی جدید"
            title = title[:160]
            raw_messages = item.get("messages", [])
            if not isinstance(raw_messages, list):
                continue
            messages = []
            for msg in raw_messages[-MAX_MESSAGES:]:
                if not isinstance(msg, dict) or msg.get("role") not in ROLES:
                    continue
                content = msg.get("content", "")
                if not isinstance(content, str):
                    content = ""
                content = content[:MAX_CONTENT]
                total += len(content.encode("utf-8"))
                if total > MAX_TOTAL:
                    raise ValueError("حجم کل تاریخچه برای ذخیره‌سازی بیش از حد مجاز است.")
                safe = {"role": msg["role"], "content": content}
                for key in ("requestId", "webSearch", "isContinue", "sources", "chart", "attachments", "error"):
                    value = msg.get(key)
                    if key == "requestId" and isinstance(value, str) and ID_RE.fullmatch(value):
                        safe[key] = value
                    elif key in ("webSearch", "isContinue") and isinstance(value, bool):
                        safe[key] = value
                    elif key == "sources" and isinstance(value, list):
                        safe[key] = value[:30]
                    elif key == "attachments" and isinstance(value, list):
                        safe[key] = [{k: str(a.get(k, ""))[:500] for k in ("id", "name", "kind", "thumb")}
                                     for a in value[:20] if isinstance(a, dict)]
                    elif key == "chart" and isinstance(value, dict):
                        safe[key] = value
                    elif key == "error" and isinstance(value, str):
                        safe[key] = value[:1000]
                messages.append(safe)
            cleaned.append({"id": cid, "title": title, "messages": messages})
        return cleaned

    def replace_all(self, items):
        cleaned = self._sanitize(items)
        now = time.time()
        with self.db.tx() as conn:
            for item in cleaned:
                encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                conn.execute("""INSERT INTO conversations(conversation_id,title,data_json,updated_at,message_count)
                    VALUES(?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET
                    title=excluded.title,data_json=excluded.data_json,updated_at=excluded.updated_at,
                    message_count=excluded.message_count""",
                    (item["id"], item["title"], encoded, now, len(item["messages"])))
            keep = {x["id"] for x in cleaned}
            if keep:
                placeholders = ",".join("?" for _ in keep)
                conn.execute(f"DELETE FROM conversations WHERE conversation_id NOT IN ({placeholders})", tuple(keep))
            else:
                # An empty browser snapshot must never erase the durable archive.
                pass
        return {"saved": len(cleaned)}

    def list_all(self):
        with self.db.read() as conn:
            rows = conn.execute("SELECT data_json FROM conversations ORDER BY updated_at DESC LIMIT ?", (MAX_CONVERSATIONS,)).fetchall()
        result = []
        for row in rows:
            try:
                result.append(json.loads(row[0]))
            except (ValueError, TypeError):
                continue
        return result
