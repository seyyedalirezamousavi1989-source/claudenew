"""Read-only SQLite inventory and migration compatibility assessment.

No database is modified. Actual migration/merge requires explicit source DBs and
an application-specific mapping; this module deliberately emits a plan only.
"""
from __future__ import annotations
import hashlib, json, sqlite3
from pathlib import Path

KNOWN_TABLES = {
    "workspace_files", "workspace_chunks", "workspace_events", "project_snapshots",
    "project_files", "project_analysis_runs", "workspace_checkpoints",
    "conversations", "messages", "attachments", "tasks", "memories",
}

def inspect_sqlite(path):
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        return {"path": str(p), "status": "missing"}
    uri = p.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            tables = [{"name": r[0], "schema": r[1] or ""} for r in rows]
            table_names = {x["name"] for x in tables}
            schema_blob = json.dumps(tables, sort_keys=True, ensure_ascii=False)
            return {"path": str(p), "status": "ok" if integrity == "ok" else "integrity_warning",
                    "integrity": integrity, "size_bytes": p.stat().st_size,
                    "tables": [x["name"] for x in tables],
                    "row_counts": _counts(conn, table_names),
                    "schema_sha256": hashlib.sha256(schema_blob.encode()).hexdigest(),
                    "recognized_tables": sorted(table_names & KNOWN_TABLES),
                    "unrecognized_tables": sorted(table_names - KNOWN_TABLES)}
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        return {"path": str(p), "status": "unreadable", "error": str(exc)[:240]}

def _counts(conn, names):
    result = {}
    for name in sorted(names):
        # Identifiers originate from sqlite_master; quote safely.
        quoted = '"' + name.replace('"', '""') + '"'
        try: result[name] = int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
        except sqlite3.Error: result[name] = None
    return result

def build_migration_plan(paths):
    reports = [inspect_sqlite(p) for p in paths]
    valid = [r for r in reports if r.get("status") == "ok"]
    by_schema = {}
    for r in valid:
        by_schema.setdefault(r["schema_sha256"], []).append(r["path"])
    return {"read_only": True, "databases": reports,
            "identical_schema_groups": [v for v in by_schema.values() if len(v) > 1],
            "migration_ready": bool(valid),
            "requires_mapping": True,
            "warning": "این گزارش فقط موجودی و سازگاری ساختاری را بررسی می‌کند؛ ادغام داده بدون تعیین مالکیت رکوردها و قواعد تعارض انجام نمی‌شود."}
