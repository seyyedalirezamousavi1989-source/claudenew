"""Persistent, incremental workspace index for uploaded documents and project archives.

The workspace stores extracted text and bounded chunks once. Chat turns retrieve only
relevant excerpts by stable attachment ID; raw uploads remain in AttachmentStore.
No archive content is executed or extracted to the filesystem here.
"""
import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

CHUNK_CHARS = 1800
CHUNK_OVERLAP = 180
MAX_CHUNKS_PER_FILE = 600
MAX_SEMANTIC_CALLS_PER_FILE = 120


def _tokens(value):
    return {x.casefold() for x in re.findall(r"[a-zA-Z0-9\u0600-\u06ff]+", str(value or "").replace("_", " ").replace(".", " ")) if len(x) > 1}


class ProjectWorkspace:
    def __init__(self, database_path):
        self._database_path = database_path
        self.path = Path(database_path() if callable(database_path) else database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init_db(self):
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workspace_files (
                    attachment_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    indexed_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_workspace_files_workspace
                    ON workspace_files(workspace_id);
                CREATE TABLE IF NOT EXISTS workspace_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attachment_id TEXT NOT NULL REFERENCES workspace_files(attachment_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    start_char INTEGER NOT NULL,
                    end_char INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    UNIQUE(attachment_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_workspace_chunks_attachment
                    ON workspace_chunks(attachment_id, ordinal);
                CREATE TABLE IF NOT EXISTS workspace_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    attachment_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_snapshots (
                    workspace_id TEXT PRIMARY KEY,
                    attachment_id TEXT NOT NULL,
                    archive_sha256 TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_files (
                    workspace_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    language TEXT,
                    extension TEXT,
                    last_seen_attachment TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, path)
                );
                CREATE INDEX IF NOT EXISTS idx_project_files_hash
                    ON project_files(workspace_id, sha256);
                CREATE TABLE IF NOT EXISTS project_analysis_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    attachment_id TEXT NOT NULL,
                    archive_sha256 TEXT NOT NULL,
                    changed_json TEXT NOT NULL,
                    impacted_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_project_analysis_workspace
                    ON project_analysis_runs(workspace_id, id DESC);
                CREATE TABLE IF NOT EXISTS workspace_checkpoints (
                    workspace_id TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(workspace_id, task_key)
                );
                CREATE TABLE IF NOT EXISTS workspace_tasks (
                    task_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_workspace_tasks_resume
                    ON workspace_tasks(workspace_id,status,updated_at DESC);
            """)

    def register(self, record, workspace_id=None):
        attachment_id = str(record.get("id") or "")
        text = str(record.get("text") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", attachment_id) or not text.strip():
            return {"indexed": False, "reason": "no_text_or_invalid_id"}
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        workspace_id = str(workspace_id or record.get("workspace_id") or "attachment:" + attachment_id)[:160]
        name = str(record.get("name") or "file")[:255]
        kind = str(record.get("kind") or "text")[:40]
        now = int(time.time())
        with self._lock, self._connect() as db:
            previous = db.execute("SELECT content_hash FROM workspace_files WHERE attachment_id=?", (attachment_id,)).fetchone()
            if previous and previous["content_hash"] == digest:
                return {"indexed": True, "changed": False, "chunks": db.execute(
                    "SELECT COUNT(*) FROM workspace_chunks WHERE attachment_id=?", (attachment_id,)
                ).fetchone()[0]}
            db.execute("""INSERT INTO workspace_files
                (attachment_id,workspace_id,name,content_hash,kind,char_count,truncated,indexed_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(attachment_id) DO UPDATE SET workspace_id=excluded.workspace_id,
                name=excluded.name,content_hash=excluded.content_hash,kind=excluded.kind,
                char_count=excluded.char_count,truncated=excluded.truncated,indexed_at=excluded.indexed_at""",
                (attachment_id, workspace_id, name, digest, kind, len(text), int(bool(record.get("truncated"))), now))
            db.execute("DELETE FROM workspace_chunks WHERE attachment_id=?", (attachment_id,))
            chunks = []
            start = 0
            while start < len(text) and len(chunks) < MAX_CHUNKS_PER_FILE:
                end = min(len(text), start + CHUNK_CHARS)
                value = text[start:end]
                if value.strip():
                    chunks.append((attachment_id, len(chunks), start, end, value))
                if end >= len(text):
                    break
                start = max(start + 1, end - CHUNK_OVERLAP)
            db.executemany("INSERT INTO workspace_chunks(attachment_id,ordinal,start_char,end_char,content) VALUES(?,?,?,?,?)", chunks)
            event = "updated" if previous else "indexed"
            db.execute("INSERT INTO workspace_events(workspace_id,attachment_id,event,created_at) VALUES(?,?,?,?)",
                       (workspace_id, attachment_id, event, now))
            manifest = record.get("project_manifest")
            project_diff = None
            if isinstance(manifest, dict) and isinstance(manifest.get("files"), list):
                prior = db.execute("SELECT manifest_json FROM project_snapshots WHERE workspace_id=?", (workspace_id,)).fetchone()
                try:
                    previous_manifest = json.loads(prior["manifest_json"]) if prior else None
                except (TypeError, ValueError):
                    previous_manifest = None
                old_files = {x.get("path"): x for x in (previous_manifest or {}).get("files", []) if isinstance(x, dict)}
                new_files = {x.get("path"): x for x in manifest.get("files", []) if isinstance(x, dict) and x.get("path")}
                added = sorted(set(new_files) - set(old_files))
                removed = sorted(set(old_files) - set(new_files))
                changed = sorted(path for path in set(old_files) & set(new_files)
                                 if old_files[path].get("sha256") != new_files[path].get("sha256"))
                unchanged = sorted(path for path in set(old_files) & set(new_files)
                                   if old_files[path].get("sha256") == new_files[path].get("sha256"))
                previous_import_graph = (previous_manifest or {}).get("import_graph") or {}
                project_diff = {"added": added, "changed": changed, "removed": removed,
                                "unchanged": unchanged, "requires_full_analysis": prior is None,
                                # Keep the old graph for one diff so deleted/renamed dependencies
                                # can still invalidate their former dependants.
                                "previous_import_graph": previous_import_graph}
                db.execute("DELETE FROM project_files WHERE workspace_id=?", (workspace_id,))
                db.executemany("""INSERT INTO project_files
                    (workspace_id,path,sha256,size,language,extension,last_seen_attachment)
                    VALUES(?,?,?,?,?,?,?)""",
                    [(workspace_id, item["path"], item.get("sha256") or "", int(item.get("size") or 0),
                      item.get("language"), item.get("extension"), attachment_id)
                     for item in new_files.values()])
                db.execute("""INSERT INTO project_snapshots(workspace_id,attachment_id,archive_sha256,manifest_json,updated_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET
                    attachment_id=excluded.attachment_id, archive_sha256=excluded.archive_sha256,
                    manifest_json=excluded.manifest_json, updated_at=excluded.updated_at""",
                    (workspace_id, attachment_id, str(manifest.get("archive_sha256") or ""),
                     json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), now))
            return {"indexed": True, "changed": bool(previous), "chunks": len(chunks), "hash": digest,
                    "project_diff": project_diff}

    def retrieve(self, attachment_ids, query, limit=4, max_chars=7000):
        ids = list(dict.fromkeys(x for x in attachment_ids if re.fullmatch(r"[0-9a-f]{32}", str(x))))[:32]
        if not ids:
            return []
        terms = _tokens(query)
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connect() as db:
            rows = db.execute(f"""SELECT c.*, f.name FROM workspace_chunks c
                JOIN workspace_files f ON f.attachment_id=c.attachment_id
                WHERE c.attachment_id IN ({placeholders})""", ids).fetchall()
        ranked = []
        for row in rows:
            body = row["content"]
            body_terms = _tokens(body)
            overlap = len(terms & body_terms) / max(1, len(terms)) if terms else 0.0
            # Small positional preference makes early project metadata discoverable without
            # overriding a clearly better semantic term match.
            score = overlap + (0.015 if row["ordinal"] == 0 else 0.0)
            ranked.append((score, row))
        ranked.sort(key=lambda item: (item[0], -item[1]["ordinal"]), reverse=True)
        out, used = [], 0
        for score, row in ranked:
            if len(out) >= max(1, min(12, int(limit))) or used >= max_chars:
                break
            if terms and score <= 0.015:
                continue
            text = row["content"]
            remaining = max_chars - used
            text = text[:remaining]
            if not text:
                continue
            out.append({"attachment_id": row["attachment_id"], "name": row["name"],
                        "start_char": row["start_char"], "end_char": row["start_char"] + len(text),
                        "score": round(score, 4), "text": text})
            used += len(text)
        # A short or cross-language follow-up may share no literal tokens with the file
        # (for example asking «توکن چی بود؟» about SECRET-TOKEN-777). In that case
        # provide one bounded opening chunk rather than silently losing file access.
        if not out and rows and max_chars > 0:
            row = min(rows, key=lambda item: (item["ordinal"], item["start_char"]))
            text = row["content"][:min(CHUNK_CHARS, max_chars)]
            if text:
                out.append({"attachment_id": row["attachment_id"], "name": row["name"],
                            "start_char": row["start_char"], "end_char": row["start_char"] + len(text),
                            "score": 0.0, "text": text})
        return out

    def status(self):
        with self._connect() as db:
            files = db.execute("SELECT COUNT(*) FROM workspace_files").fetchone()[0]
            chunks = db.execute("SELECT COUNT(*) FROM workspace_chunks").fetchone()[0]
            chars = db.execute("SELECT COALESCE(SUM(char_count),0) FROM workspace_files").fetchone()[0]
        return {"files": files, "chunks": chunks, "characters_indexed": chars, "database": str(self.path)}

    def project_manifest(self, workspace_id):
        with self._connect() as db:
            row = db.execute("SELECT manifest_json FROM project_snapshots WHERE workspace_id=?", (str(workspace_id),)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["manifest_json"])
        except (TypeError, ValueError):
            return None

    def project_diff(self, workspace_id, attachment_id):
        """Return the latest persisted snapshot metadata for diagnostics and UI."""
        manifest = self.project_manifest(workspace_id)
        if not manifest:
            return None
        with self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM project_files WHERE workspace_id=?", (str(workspace_id),)).fetchone()[0]
        return {"workspace_id": str(workspace_id), "attachment_id": attachment_id,
                "files": count, "languages": manifest.get("languages", []),
                "frameworks": manifest.get("frameworks", []), "entrypoints": manifest.get("entrypoints", []),
                "coverage": manifest.get("coverage", 0), "skipped": manifest.get("skipped", [])}

    def analyze_project(self, workspace_id, attachment_id, manifest, diff=None):
        """Persist bounded static semantic metadata for changed/impacted files.

        Source code is never imported or executed. Python metadata comes from AST;
        other supported languages currently use conservative lexical extraction.
        """
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
            return {"analyzed": False, "reason": "manifest_unavailable"}
        diff = diff or {"added": [x.get("path") for x in manifest["files"]],
                        "changed": [], "removed": [], "unchanged": [],
                        "requires_full_analysis": True}
        files = {x.get("path"): x for x in manifest["files"] if isinstance(x, dict) and x.get("path")}
        graph = manifest.get("import_graph") or {}
        # Dependency-aware invalidation uses both graph generations. This matters when
        # a dependency was removed or renamed: the current graph alone has lost that edge.
        reverse = {}
        for dependency_graph in (diff.get("previous_import_graph") or {}, graph):
            for source, dependencies in dependency_graph.items():
                for dependency in dependencies if isinstance(dependencies, list) else []:
                    reverse.setdefault(dependency, set()).add(source)
        changed = set(diff.get("added", [])) | set(diff.get("changed", [])) | set(diff.get("removed", []))
        if diff.get("requires_full_analysis"):
            impacted = set(files)
        else:
            impacted = set(changed)
            queue = list(changed)
            while queue:
                node = queue.pop()
                for dependent in reverse.get(node, ()):
                    if dependent not in impacted:
                        impacted.add(dependent)
                        queue.append(dependent)
            impacted.intersection_update(files)
        task = self.create_task(str(workspace_id), "dependency_aware_analysis", {
            "attachment_id": str(attachment_id), "archive_sha256": str(manifest.get("archive_sha256") or ""),
            "changed_paths": sorted(changed), "impacted_paths": sorted(impacted),
            "removed_paths": sorted(set(diff.get("removed", []))),
        })
        self.update_task(task["task_id"], status="running", stage="collecting_symbols", progress=0.1)
        symbols, semantic_files, routes, calls = [], [], [], []
        for path in sorted(impacted):
            item = files.get(path) or {}
            semantic = item.get("semantic") if isinstance(item.get("semantic"), dict) else {}
            for symbol in item.get("symbols", [])[:100]:
                if isinstance(symbol, dict) and symbol.get("name"):
                    symbols.append({"path": path, "kind": symbol.get("kind"),
                                    "name": str(symbol["name"])[:160], "line": symbol.get("line"),
                                    "parent": symbol.get("parent"), "doc": str(symbol.get("doc") or "")[:240]})
            if semantic:
                semantic_files.append({"path": path, "status": semantic.get("status"),
                                       "parser": semantic.get("parser"),
                                       "symbol_count": semantic.get("symbol_count", len(semantic.get("symbols", []))),
                                       "call_count": semantic.get("call_count", len(semantic.get("calls", []))),
                                       "limitations": semantic.get("limitations", [])[:3]})
                for route in semantic.get("routes", [])[:100]:
                    routes.append({"path": path, **route})
                for call in semantic.get("calls", [])[:MAX_SEMANTIC_CALLS_PER_FILE]:
                    calls.append({"path": path, **call})
        try:
            from .cross_file_analysis import build_cross_file_index
        except ImportError:
            from cross_file_analysis import build_cross_file_index
        self.update_task(task["task_id"], stage="building_cross_file_index", progress=0.65)
        cross_file = build_cross_file_index(manifest)
        summary = {
            "workspace_id": str(workspace_id), "attachment_id": str(attachment_id),
            "languages": manifest.get("languages", []), "frameworks": manifest.get("frameworks", []),
            "entrypoints": manifest.get("entrypoints", []), "coverage": manifest.get("coverage", 0),
            "files_total": len(files), "files_impacted": sorted(impacted),
            "symbols": symbols[:1200],
            "semantic_files": semantic_files[:1200],
            "routes": routes[:400],
            "calls": calls[:1200],
            "cross_file_index": cross_file,
            "dependency_invalidation": {"changed_paths": sorted(changed),
                "impacted_paths": sorted(impacted), "removed_paths": sorted(set(diff.get("removed", []))),
                "dependency_graph_generations": 2 if diff.get("previous_import_graph") else 1},
            "syntax_errors": [x for x in manifest.get("syntax_errors", []) if x.get("path") in impacted],
            "import_graph": {k: v for k, v in graph.items() if k in impacted},
            "analysis_scope": "static_metadata_only",
            "limitations": ["کد پروژه اجرا یا import نشده است.",
                            "Python با AST تحلیل شده؛ زبان‌های دیگر عمدتاً تحلیل واژگانی محدود دارند.",
                            "حل کامل نوع‌ها، ارجاع‌های پویا و Call Graph بین‌فایلی انجام نمی‌شود."]
        }
        now = int(time.time())
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO project_analysis_runs
                (workspace_id,attachment_id,archive_sha256,changed_json,impacted_json,summary_json,created_at)
                VALUES(?,?,?,?,?,?,?)""", (str(workspace_id), str(attachment_id),
                str(manifest.get("archive_sha256") or ""), json.dumps(diff, ensure_ascii=False),
                json.dumps(sorted(impacted), ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False), now))
        checkpoint = {
            "schema_version": 2, "status": "completed", "task_id": task["task_id"],
            "attachment_id": str(attachment_id), "archive_sha256": str(manifest.get("archive_sha256") or ""),
            "changed_count": len(changed), "impacted_count": len(impacted),
            "impacted_paths": sorted(impacted), "analysis_scope": "static_metadata_only", "completed_at": now,
        }
        self.save_checkpoint(str(workspace_id), "semantic_analysis", checkpoint)
        self.update_task(task["task_id"], status="completed", stage="completed", progress=1.0,
                         payload={**task["payload"], "result_checkpoint": "semantic_analysis"})
        return {"analyzed": True, "task_id": task["task_id"],
                "full_analysis": bool(diff.get("requires_full_analysis")),
                "changed_count": len(changed), "impacted_count": len(impacted),
                "impacted_paths": sorted(impacted), "summary": summary}

    def project_context(self, workspace_id, query="", max_chars=2600):
        """Return a compact, bounded, restart-safe project context block."""
        manifest = self.project_manifest(workspace_id)
        if not manifest:
            return ""
        with self._connect() as db:
            row = db.execute("""SELECT summary_json FROM project_analysis_runs
                WHERE workspace_id=? ORDER BY id DESC LIMIT 1""", (str(workspace_id),)).fetchone()
        try:
            analysis = json.loads(row["summary_json"]) if row else {}
        except (TypeError, ValueError):
            analysis = {}
        terms = _tokens(query)
        ranked = []
        # Search symbols across the full current manifest, not only the most recent
        # incremental run (which intentionally contains changed files only).
        symbol_hits = []
        for file_item in manifest.get("files", []):
            if not isinstance(file_item, dict):
                continue
            for symbol in file_item.get("symbols", [])[:100]:
                if not isinstance(symbol, dict):
                    continue
                enriched = dict(symbol)
                enriched["path"] = file_item.get("path")
                searchable = _tokens(" ".join(str(enriched.get(k) or "") for k in ("name", "kind", "parent", "doc", "path")))
                score = len(terms & searchable)
                if score:
                    symbol_hits.append((score, enriched))
        symbol_hits.sort(key=lambda x: (-x[0], str(x[1].get("path")), str(x[1].get("name"))))
        for item in manifest.get("files", []):
            path = str(item.get("path") or "")
            path_terms = _tokens(path)
            names = _tokens(" ".join(str(x.get("name") or "") for x in item.get("symbols", []) if isinstance(x, dict)))
            score = len(terms & path_terms) * 2 + len(terms & names)
            if score:
                ranked.append((score, path, item))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        selected = [x[2] for x in ranked[:12]]
        if not selected:
            selected = [x for x in manifest.get("files", []) if x.get("path") in set(manifest.get("entrypoints", []))][:8]
        lines = ["[Project Workspace: نمای ساختاری بازیابی‌شده؛ منبع آن ZIP ثبت‌شده است]"]
        lines.append("زبان‌ها: " + ", ".join(manifest.get("languages", [])[:12]))
        lines.append("فریم‌ورک‌ها: " + ", ".join(manifest.get("frameworks", [])[:12]))
        lines.append("نقاط ورود: " + ", ".join(manifest.get("entrypoints", [])[:12]))
        lines.append(f"پوشش Manifest: {manifest.get('coverage', 0)}؛ فایل‌های ثبت‌شده: {len(manifest.get('files', []))}")
        if analysis.get("files_impacted"):
            lines.append("آخرین تحلیل ساختاری روی فایل‌ها: " + ", ".join(analysis["files_impacted"][:20]))
        if selected:
            lines.append("فایل‌های مرتبط/مهم: " + "; ".join(
                f"{x.get('path')} ({x.get('language') or x.get('extension') or 'unknown'})" for x in selected))
        if symbol_hits:
            lines.append("نمادهای مرتبط با پرسش: " + "; ".join(
                f"{x.get('path')}::{x.get('name')} ({x.get('kind')}, خط {x.get('line')})"
                for _, x in symbol_hits[:10]))
        cross_file = analysis.get("cross_file_index") or {}
        if cross_file.get("possible_call_edges"):
            query_terms = terms
            edges = cross_file["possible_call_edges"]
            if query_terms:
                edges = [edge for edge in edges if _tokens(edge.get("symbol")) & query_terms or
                         _tokens(edge.get("source")) & query_terms or _tokens(edge.get("target")) & query_terms]
            if edges:
                lines.append("ارجاع‌های بین‌فایلی احتمالی: " + "; ".join(
                    f"{edge.get('source')}:{edge.get('line')} → {edge.get('target')}::{edge.get('symbol')}"
                    for edge in edges[:8]))
        if analysis.get("routes"):
            lines.append("مسیرهای API شناسایی‌شده: " + "; ".join(
                f"{x.get('path')}:{x.get('line')} {x.get('method') or x.get('decorator') or ''} {x.get('path') if x.get('method') else x.get('function', '')}"
                for x in analysis["routes"][:8]))
        if analysis.get("syntax_errors"):
            lines.append("خطاهای نحوی شناسایی‌شده: " + "; ".join(
                f"{x.get('path')}:{x.get('line')}" for x in analysis["syntax_errors"][:8]))
        if analysis.get("limitations"):
            lines.append("محدودیت تحلیل: " + "؛ ".join(str(x) for x in analysis["limitations"][:3]))
        text = "\n".join(lines)
        return text[:max(0, int(max_chars))]

    def save_checkpoint(self, workspace_id, task_key, state):
        """Upsert resumable task state; callers must store only non-secret metadata."""
        workspace_id, task_key = str(workspace_id)[:160], str(task_key)[:160]
        payload = json.dumps(state if isinstance(state, dict) else {"value": state},
                             ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO workspace_checkpoints(workspace_id,task_key,state_json,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(workspace_id,task_key) DO UPDATE SET
                state_json=excluded.state_json,updated_at=excluded.updated_at""",
                (workspace_id, task_key, payload, int(time.time())))
        return True

    def load_checkpoint(self, workspace_id, task_key):
        with self._connect() as db:
            row = db.execute("SELECT state_json FROM workspace_checkpoints WHERE workspace_id=? AND task_key=?",
                             (str(workspace_id)[:160], str(task_key)[:160])).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["state_json"])
        except (TypeError, ValueError):
            return None

    def list_checkpoints(self, workspace_id, limit=50):
        """List resumable workspace checkpoints newest first, without exposing raw files."""
        limit = max(1, min(200, int(limit or 50)))
        with self._connect() as db:
            rows = db.execute("""SELECT task_key,state_json,updated_at FROM workspace_checkpoints
                WHERE workspace_id=? ORDER BY updated_at DESC LIMIT ?""",
                (str(workspace_id)[:160], limit)).fetchall()
        result = []
        for row in rows:
            try:
                state = json.loads(row["state_json"])
            except (TypeError, ValueError):
                state = None
            result.append({"task_key": row["task_key"], "state": state, "updated_at": row["updated_at"]})
        return result

    def create_task(self, workspace_id, task_type, payload=None, task_id=None):
        """Create a durable resumable task record. Payload must contain metadata, not secrets."""
        import uuid
        task_id = str(task_id or uuid.uuid4().hex)[:64]
        now = int(time.time())
        safe_payload = payload if isinstance(payload, dict) else {}
        encoded = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO workspace_tasks
                (task_id,workspace_id,task_type,status,stage,progress,payload_json,error,created_at,updated_at)
                VALUES(?,?,?,'queued','queued',0,?,NULL,?,?)""",
                (task_id, str(workspace_id)[:160], str(task_type)[:80], encoded, now, now))
        return self.get_task(task_id)

    def update_task(self, task_id, *, status=None, stage=None, progress=None, payload=None, error=None):
        """Persist task progress atomically; completed/failed/cancelled are terminal states."""
        allowed = {"queued", "running", "completed", "failed", "cancelled"}
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM workspace_tasks WHERE task_id=?", (str(task_id),)).fetchone()
            if not row:
                return None
            current = row["status"]
            new_status = status or current
            if new_status not in allowed:
                raise ValueError("invalid task status")
            if current in {"completed", "failed", "cancelled"} and new_status == "running":
                raise ValueError("terminal task cannot be restarted; create a resume task")
            new_progress = row["progress"] if progress is None else max(0.0, min(1.0, float(progress)))
            if new_status == "completed":
                new_progress = 1.0
            new_payload = row["payload_json"] if payload is None else json.dumps(
                payload if isinstance(payload, dict) else {}, ensure_ascii=False, separators=(",", ":"))
            db.execute("""UPDATE workspace_tasks SET status=?,stage=?,progress=?,payload_json=?,error=?,updated_at=?
                WHERE task_id=?""", (new_status, str(stage or row["stage"])[:100], new_progress,
                new_payload, str(error)[:1000] if error else None, int(time.time()), str(task_id)))
        return self.get_task(task_id)

    def get_task(self, task_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM workspace_tasks WHERE task_id=?", (str(task_id),)).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        return {"task_id": row["task_id"], "workspace_id": row["workspace_id"],
                "task_type": row["task_type"], "status": row["status"], "stage": row["stage"],
                "progress": row["progress"], "payload": payload, "error": row["error"],
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def resumable_tasks(self, workspace_id=None, limit=50):
        """Find queued/running tasks after restart; stale running work is visible, never silently lost."""
        limit = max(1, min(200, int(limit or 50)))
        query = "SELECT task_id FROM workspace_tasks WHERE status IN ('queued','running')"
        params = []
        if workspace_id is not None:
            query += " AND workspace_id=?"
            params.append(str(workspace_id)[:160])
        query += " ORDER BY updated_at ASC LIMIT ?"
        params.append(limit)
        with self._connect() as db:
            ids = [row[0] for row in db.execute(query, params).fetchall()]
        return [self.get_task(task_id) for task_id in ids]

    def resume_task(self, task_id):
        """Mark an interrupted task as running again and return its persisted checkpoint."""
        task = self.get_task(task_id)
        if not task or task["status"] not in {"queued", "running"}:
            return None
        return self.update_task(task_id, status="running", stage="resuming")

    def remove(self, attachment_id):
        with self._lock, self._connect() as db:
            cur = db.execute("DELETE FROM workspace_files WHERE attachment_id=?", (attachment_id,))
            return cur.rowcount > 0
