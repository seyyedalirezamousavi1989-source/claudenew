from local_chat.project_workspace import ProjectWorkspace


def test_workspace_indexes_once_and_reuses_hash(tmp_path):
    store = ProjectWorkspace(tmp_path / "workspace.sqlite3")
    record = {"id": "a" * 32, "name": "guide.txt", "kind": "document",
              "text": "مقدمه درباره کتابخانه پایتون.\n\n" + ("بخش نصب و راه اندازی محیط پایتون شامل مراحل نصب است. " * 100)}
    first = store.register(record)
    second = store.register(record)
    assert first["indexed"] is True
    assert first["changed"] is False
    assert second["changed"] is False
    assert store.status()["files"] == 1
    assert store.status()["chunks"] >= 1


def test_workspace_retrieves_relevant_chunk_and_updates_incrementally(tmp_path):
    store = ProjectWorkspace(tmp_path / "workspace.sqlite3")
    record = {"id": "b" * 32, "name": "project.txt", "kind": "archive",
              "text": ("مستندات رابط کاربری و طراحی ظاهری. " * 100) +
                      ("خطای اتصال پایگاه داده SQLite و تنظیمات دیتابیس. " * 100)}
    store.register(record)
    found = store.retrieve([record["id"]], "خطای اتصال پایگاه داده SQLite", limit=2)
    assert found
    assert "SQLite" in found[0]["text"]
    changed = dict(record, text=record["text"] + "\nنسخه جدید")
    update = store.register(changed)
    assert update["changed"] is True
    assert store.status()["files"] == 1


def test_workspace_rejects_invalid_attachment_ids(tmp_path):
    store = ProjectWorkspace(tmp_path / "workspace.sqlite3")
    assert store.register({"id": "../bad", "text": "hello"})["indexed"] is False
    assert store.retrieve(["../bad"], "hello") == []


def test_project_analysis_recomputes_changed_file_and_reverse_dependents(tmp_path):
    store = ProjectWorkspace(tmp_path / "workspace.sqlite3")
    manifest = {
        "archive_sha256": "abc", "files": [
            {"path": "app.py", "sha256": "1", "symbols": [{"kind": "function", "name": "main", "line": 1}]},
            {"path": "service.py", "sha256": "2", "symbols": [{"kind": "function", "name": "run", "line": 2}]},
            {"path": "util.py", "sha256": "3", "symbols": []},
        ],
        "import_graph": {"app.py": ["service.py"], "service.py": ["util.py"]},
        "languages": ["Python"], "frameworks": ["FastAPI"], "entrypoints": ["app.py"],
        "coverage": 1.0, "syntax_errors": []
    }
    result = store.analyze_project("project:test", "a" * 32, manifest,
        {"added": [], "changed": ["util.py"], "removed": [], "unchanged": ["app.py", "service.py"], "requires_full_analysis": False})
    assert result["analyzed"] is True
    assert result["summary"]["files_impacted"] == ["app.py", "service.py", "util.py"]
    assert result["summary"]["symbols"][0]["name"] in {"main", "run"}


def test_workspace_checkpoints_survive_reopen(tmp_path):
    path = tmp_path / "workspace.sqlite3"
    first = ProjectWorkspace(path)
    first.save_checkpoint("project:one", "analysis", {"phase": "symbols", "completed": ["a.py"]})
    reopened = ProjectWorkspace(path)
    assert reopened.load_checkpoint("project:one", "analysis") == {"phase": "symbols", "completed": ["a.py"]}


def test_project_context_is_bounded_and_contains_manifest_summary(tmp_path):
    store = ProjectWorkspace(tmp_path / "workspace.sqlite3")
    manifest = {"archive_sha256": "hash", "files": [{"path": "manage.py", "sha256": "x", "language": "Python"}],
                "languages": ["Python"], "frameworks": ["Django"], "entrypoints": ["manage.py"],
                "coverage": 1.0, "import_graph": {}, "syntax_errors": []}
    store.register({"id": "c" * 32, "name": "site.zip", "kind": "archive", "text": "manifest",
                    "workspace_id": "project:site", "project_manifest": manifest})
    context = store.project_context("project:site", "Django project", max_chars=500)
    assert len(context) <= 500
    assert "Django" in context
