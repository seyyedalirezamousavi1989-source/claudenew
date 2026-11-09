import sqlite3
from local_chat.cross_file_analysis import build_cross_file_index
from local_chat.db_migration_audit import inspect_sqlite, build_migration_plan
from local_chat.project_workspace import ProjectWorkspace


def test_cross_file_possible_call_and_ambiguous_reference():
    manifest = {"files": [
        {"path": "services/billing.py", "extension": ".py", "symbols": [
            {"name": "calculate_total", "kind": "function", "line": 3}],
            "semantic": {"calls": [{"name": "calculate_total", "line": 12, "scope": "checkout"}]}},
        {"path": "views/checkout.py", "extension": ".py", "symbols": [],
            "semantic": {"calls": [{"name": "billing.calculate_total", "line": 9, "scope": "view"}]}},
    ], "import_graph": {"views/checkout.py": ["services/billing.py"]}}
    index = build_cross_file_index(manifest)
    assert any(x["source"] == "views/checkout.py" and x["target"] == "services/billing.py"
               and x["symbol"] == "calculate_total" for x in index["possible_call_edges"])
    assert index["import_edges"]


def test_workspace_persists_cross_file_index_and_context(tmp_path):
    store = ProjectWorkspace(tmp_path / "workspace.sqlite3")
    manifest = {"archive_sha256": "a"*64, "languages": ["Python"], "frameworks": [],
        "entrypoints": ["views.py"], "coverage": 1.0, "skipped": [],
        "import_graph": {"views.py": ["billing.py"]}, "files": [
            {"path": "billing.py", "sha256": "b"*64, "size": 1, "extension": ".py",
             "symbols": [{"name": "calculate_total", "kind": "function", "line": 2}],
             "semantic": {"status": "ok", "symbols": [{"name": "calculate_total", "kind": "function", "line": 2}], "calls": [], "routes": []}},
            {"path": "views.py", "sha256": "c"*64, "size": 1, "extension": ".py", "symbols": [],
             "semantic": {"status": "ok", "symbols": [], "calls": [{"name": "calculate_total", "line": 8, "scope": "view"}], "routes": []}},
        ]}
    store.register({"id": "1"*32, "name": "project.zip", "kind": "archive", "text": "source",
                    "workspace_id": "project:x", "project_manifest": manifest})
    store.analyze_project("project:x", "1"*32, manifest)
    context = store.project_context("project:x", "calculate total", 2500)
    assert "views.py" in context and "billing.py" in context
    with store._connect() as db:
        row = db.execute("SELECT summary_json FROM project_analysis_runs WHERE workspace_id='project:x'").fetchone()
    assert 'cross_file_index' in row['summary_json']


def test_database_audit_is_read_only_and_reports_schema(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE conversations(id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO conversations(title) VALUES('old chat')")
    conn.commit(); conn.close()
    before = path.read_bytes()
    report = inspect_sqlite(path)
    assert report["status"] == "ok"
    assert report["row_counts"]["conversations"] == 1
    assert "conversations" in report["recognized_tables"]
    assert path.read_bytes() == before
    plan = build_migration_plan([path, tmp_path / "missing.db"])
    assert plan["read_only"] is True and plan["databases"][1]["status"] == "missing"
