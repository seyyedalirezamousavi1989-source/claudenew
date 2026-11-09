import io
import zipfile

from local_chat.semantic_code_analysis import analyze_source
from local_chat.zip_intelligence import inspect_zip
from local_chat.project_workspace import ProjectWorkspace


def test_python_ast_indexes_nested_symbols_calls_and_routes():
    source = b'''\nfrom fastapi import FastAPI\napp = FastAPI()\nclass UserService:\n    def get_user(self, user_id: int):\n        return fetch_user(user_id)\n@app.get("/users/{user_id}")\nasync def user_detail(user_id: int):\n    return service.get_user(user_id)\n'''
    result = analyze_source("app.py", source)
    assert result["status"] == "ok"
    assert {x["name"] for x in result["symbols"]} >= {"UserService", "get_user", "user_detail"}
    assert "fastapi" in result["imports"]
    assert any("fetch_user" in x["name"] for x in result["calls"])
    assert any("/users/" in x["decorator"] for x in result["routes"])


def test_js_ts_bounded_lexical_symbols_imports_and_routes():
    source = b'''import { Router } from "express";\nexport class Catalog {}\nexport const listItems = async () => [];\nrouter.get("/items", listItems);'''
    result = analyze_source("routes.ts", source)
    assert "express" in result["imports"]
    assert {x["name"] for x in result["symbols"]} >= {"Catalog", "listItems"}
    assert result["routes"][0]["path"] == "/items"
    assert result["parser"] == "bounded_lexical"


def test_zip_manifest_embeds_semantic_metadata():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("app.py", "class Store:\n    def get(self):\n        return load()\n")
    manifest = inspect_zip(buffer.getvalue())
    item = next(x for x in manifest["files"] if x["path"] == "app.py")
    assert item["semantic"]["parser"] == "python_ast"
    assert any(x["name"] == "get" for x in item["symbols"])


def test_workspace_context_retrieves_semantic_symbol_hits(tmp_path):
    store = ProjectWorkspace(tmp_path / "workspace.sqlite3")
    manifest = {
        "archive_sha256": "a" * 64,
        "languages": ["Python"], "frameworks": ["FastAPI"], "entrypoints": ["app.py"],
        "coverage": 1.0, "skipped": [], "import_graph": {},
        "files": [{"path": "app.py", "sha256": "b" * 64, "size": 100,
                   "extension": ".py", "language": "Python", "imports": [],
                   "symbols": [{"kind": "function", "name": "calculate_invoice", "line": 12,
                                "parent": None, "doc": "Calculate invoice total"}],
                   "semantic": {"status": "ok", "parser": "python_ast", "symbols": [
                       {"kind": "function", "name": "calculate_invoice", "line": 12,
                        "parent": None, "doc": "Calculate invoice total"}],
                       "calls": [], "routes": [], "symbol_count": 1, "call_count": 0}}]
    }
    record = {"id": "1" * 32, "name": "project.zip", "kind": "archive", "text": "project source",
              "workspace_id": "project:test", "project_manifest": manifest}
    registered = store.register(record)
    assert registered["indexed"]
    store.analyze_project("project:test", "1" * 32, manifest, registered.get("project_diff"))
    context = store.project_context("project:test", "where is calculate_invoice", max_chars=2000)
    assert "calculate_invoice" in context


def test_incremental_context_keeps_symbols_from_unchanged_files(tmp_path):
    store = ProjectWorkspace(tmp_path / "workspace.sqlite3")
    first = {"archive_sha256": "1" * 64, "languages": ["Python"], "frameworks": [],
             "entrypoints": [], "coverage": 1.0, "skipped": [], "import_graph": {},
             "files": [
                 {"path": "billing.py", "sha256": "a" * 64, "size": 10, "extension": ".py", "language": "Python",
                  "symbols": [{"kind": "function", "name": "calculate_invoice", "line": 4}], "semantic": {"status": "ok"}},
                 {"path": "users.py", "sha256": "b" * 64, "size": 10, "extension": ".py", "language": "Python",
                  "symbols": [{"kind": "function", "name": "get_user", "line": 8}], "semantic": {"status": "ok"}},
             ]}
    first_record = {"id": "2" * 32, "name": "one.zip", "kind": "archive", "text": "first",
                    "workspace_id": "project:incremental", "project_manifest": first}
    store.register(first_record)
    store.analyze_project("project:incremental", "2" * 32, first)

    second = {**first, "archive_sha256": "3" * 64, "files": [
        first["files"][0],
        {**first["files"][1], "sha256": "c" * 64,
         "symbols": [{"kind": "function", "name": "fetch_user", "line": 9}]},
    ]}
    second_record = {"id": "3" * 32, "name": "two.zip", "kind": "archive", "text": "second",
                     "workspace_id": "project:incremental", "project_manifest": second}
    registration = store.register(second_record)
    store.analyze_project("project:incremental", "3" * 32, second, registration.get("project_diff"))
    context = store.project_context("project:incremental", "calculate invoice", max_chars=2000)
    assert "calculate_invoice" in context
    checkpoints = store.list_checkpoints("project:incremental")
    assert checkpoints and checkpoints[0]["task_key"] == "semantic_analysis"
    assert checkpoints[0]["state"]["status"] == "completed"


def test_dependency_aware_invalidation_uses_previous_and_current_graph(tmp_path):
    store = ProjectWorkspace(tmp_path / "workspace.sqlite3")
    first = {
        "archive_sha256": "1" * 64, "languages": ["Python"], "frameworks": [],
        "entrypoints": [], "coverage": 1.0, "skipped": [],
        "import_graph": {"app.py": ["service.py"], "service.py": ["db.py"], "db.py": []},
        "files": [
            {"path": path, "sha256": (str(i) * 64)[:64], "size": 10, "extension": ".py",
             "language": "Python", "symbols": [], "semantic": {"status": "ok", "symbols": [], "calls": [], "routes": []}}
            for i, path in enumerate(("app.py", "service.py", "db.py"), 1)
        ],
    }
    store.register({"id": "a" * 32, "name": "one.zip", "kind": "archive", "text": "one",
                    "workspace_id": "project:deps", "project_manifest": first})
    store.analyze_project("project:deps", "a" * 32, first)
    # db.py is removed and the current graph no longer contains the old chain.
    second = {**first, "archive_sha256": "2" * 64,
              "import_graph": {"app.py": ["service.py"], "service.py": [], "app.py": []},
              "files": [first["files"][0], first["files"][1]]}
    registered = store.register({"id": "b" * 32, "name": "two.zip", "kind": "archive", "text": "two",
                                 "workspace_id": "project:deps", "project_manifest": second})
    result = store.analyze_project("project:deps", "b" * 32, second, registered["project_diff"])
    assert "service.py" in result["impacted_paths"]
    assert "app.py" in result["impacted_paths"]
    assert "db.py" not in result["impacted_paths"]


def test_workspace_tasks_survive_restart_and_are_resumable(tmp_path):
    db_path = tmp_path / "workspace.sqlite3"
    store = ProjectWorkspace(db_path)
    task = store.create_task("project:resume", "long_analysis", {"phase": "index", "cursor": 7}, "task-resume-1")
    store.update_task(task["task_id"], status="running", stage="extracting", progress=0.4)
    # Re-open a new store instance to simulate application restart.
    restarted = ProjectWorkspace(db_path)
    pending = restarted.resumable_tasks("project:resume")
    assert len(pending) == 1
    assert pending[0]["task_id"] == "task-resume-1"
    assert pending[0]["payload"] == {"phase": "index", "cursor": 7}
    resumed = restarted.resume_task("task-resume-1")
    assert resumed["status"] == "running"
    assert resumed["stage"] == "resuming"
