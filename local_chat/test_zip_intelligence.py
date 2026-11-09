import io
import zipfile

import pytest

try:
    from .zip_intelligence import inspect_zip, compare_manifests
    from .project_workspace import ProjectWorkspace
except ImportError:
    from zip_intelligence import inspect_zip, compare_manifests
    from project_workspace import ProjectWorkspace


def make_zip(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return out.getvalue()


def test_manifest_detects_languages_frameworks_and_entrypoints():
    data = make_zip({
        "manage.py": "print('django')",
        "requirements.txt": "Django>=5.2\nPillow\n",
        "shop/models.py": "class Product: pass\n",
        "static/site.css": "body{}",
        "node_modules/pkg/index.js": "ignored",
    })
    manifest = inspect_zip(data, "shop.zip")
    assert "Python" in manifest["languages"]
    assert "CSS" in manifest["languages"]
    assert "Django" in manifest["frameworks"]
    assert "manage.py" in manifest["entrypoints"]
    assert any(x["reason"] == "generated_or_vendor_directory" for x in manifest["skipped"])
    assert manifest["coverage"] < 1


def test_manifest_rejects_path_traversal():
    data = make_zip({"../outside.txt": "nope", "safe/readme.md": "ok"})
    manifest = inspect_zip(data)
    assert any(x["reason"] == "unsafe_path" for x in manifest["skipped"])
    assert [x["path"] for x in manifest["files"]] == ["safe/readme.md"]


def test_incremental_manifest_diff():
    before = inspect_zip(make_zip({"a.py": "one", "b.py": "same"}))
    after = inspect_zip(make_zip({"a.py": "two", "b.py": "same", "c.py": "new"}))
    diff = compare_manifests(before, after)
    assert diff["changed"] == ["a.py"]
    assert diff["added"] == ["c.py"]
    assert diff["unchanged"] == ["b.py"]
    assert diff["removed"] == []


def test_workspace_persists_project_manifest_and_file_diff(tmp_path):
    workspace = ProjectWorkspace(tmp_path / "workspace.sqlite3")
    first = inspect_zip(make_zip({"a.py": "one", "b.py": "same"}), "sample.zip")
    record1 = {"id": "a" * 32, "name": "sample.zip", "kind": "archive", "text": "project text",
               "workspace_id": "project:sample", "project_manifest": first}
    result1 = workspace.register(record1)
    assert result1["project_diff"]["requires_full_analysis"] is True
    second = inspect_zip(make_zip({"a.py": "two", "b.py": "same", "c.py": "new"}), "sample.zip")
    record2 = {"id": "b" * 32, "name": "sample.zip", "kind": "archive", "text": "updated project text",
               "workspace_id": "project:sample", "project_manifest": second}
    result2 = workspace.register(record2)
    assert result2["project_diff"]["changed"] == ["a.py"]
    assert result2["project_diff"]["added"] == ["c.py"]
    assert result2["project_diff"]["unchanged"] == ["b.py"]
    assert workspace.project_manifest("project:sample")["archive_sha256"] == second["archive_sha256"]


def test_python_import_graph_and_syntax_diagnostics():
    data = make_zip({
        "src/main.py": "from src.services import run\nif __name__ == '__main__': run()\n",
        "src/services.py": "def run():\n    return True\n",
        "src/broken.py": "def broken(:\n    pass\n",
    })
    manifest = inspect_zip(data)
    assert manifest["import_graph"]["src/main.py"] == ["src/services.py"]
    assert manifest["syntax_errors"][0]["path"] == "src/broken.py"


def test_upload_attaches_project_manifest():
    try:
        from . import files
    except ImportError:
        import files
    data = make_zip({"manage.py": "print('django')", "requirements.txt": "Django>=5.2"})
    record = files.process_upload("sample-project.zip", data)
    assert record["kind"] == "archive"
    assert record["project_manifest"]["indexed_entries"] == 2
    assert record["workspace_id"].startswith("project:")
    assert "Project manifest summary" in record["text"]
