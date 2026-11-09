"""Safe, read-only ZIP project inventory and incremental change detection.

Never extracts archive members to disk or executes project content.  The manifest is
bounded and intentionally stores metadata only; document text remains in the attachment
store / workspace chunk index.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import PurePosixPath

try:
    from .semantic_code_analysis import analyze_source
except ImportError:
    from semantic_code_analysis import analyze_source

MAX_ENTRIES = 20_000
MAX_DECLARED_BYTES = 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_FILES = 12_000
MAX_COMPRESSION_RATIO = 250
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}
LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript/React", ".ts": "TypeScript",
    ".tsx": "TypeScript/React", ".java": "Java", ".kt": "Kotlin", ".go": "Go", ".rs": "Rust",
    ".cs": "C#", ".php": "PHP", ".rb": "Ruby", ".swift": "Swift", ".dart": "Dart",
    ".c": "C", ".h": "C/C++", ".cpp": "C++", ".sql": "SQL", ".html": "HTML", ".css": "CSS",
    ".scss": "SCSS", ".vue": "Vue", ".svelte": "Svelte", ".sh": "Shell", ".ps1": "PowerShell",
}
FRAMEWORK_MARKERS = {
    "Django": ("manage.py", "django"), "Flask": ("flask",), "FastAPI": ("fastapi",),
    "React": ("react",), "Next.js": ("next",), "Vue": ("vue",), "Angular": ("@angular/core",),
    "Svelte": ("svelte",), "Express": ("express",), "NestJS": ("@nestjs/core",),
    "Laravel": ("laravel/framework",), "Symfony": ("symfony/framework-bundle",),
    "Spring Boot": ("spring-boot",), "ASP.NET": ("microsoft.aspnetcore",),
    "Flutter": ("flutter",), "FastAPI": ("fastapi",),
}
ENTRY_NAMES = {"manage.py", "main.py", "app.py", "wsgi.py", "asgi.py", "index.js", "index.ts", "server.js", "server.ts", "program.cs", "main.go", "Cargo.toml", "package.json"}


def _safe_member(name: str) -> str | None:
    name = str(name or "").replace("\\", "/")
    p = PurePosixPath(name)
    if not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    if any(part in ("", ".", "..") for part in p.parts):
        return None
    return p.as_posix()


def inspect_zip(data: bytes, archive_name: str = "project.zip") -> dict:
    """Create a bounded manifest and summary. Raises ValueError for unsafe archives."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("ZIP خراب یا نامعتبر است") from exc
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_ENTRIES:
        raise ValueError(f"تعداد فایل‌های ZIP از حد مجاز بیشتر است ({len(infos)})")
    declared = sum(max(0, i.file_size) for i in infos)
    if declared > MAX_DECLARED_BYTES:
        raise ValueError("حجم بازشده ZIP از حد مجاز بیشتر است")

    files, skipped, warnings = [], [], []
    languages, frameworks, dependencies, entrypoints = set(), set(), set(), []
    for info in infos:
        path = _safe_member(info.filename)
        if not path:
            skipped.append({"path": info.filename[:300], "reason": "unsafe_path"})
            continue
        parts = PurePosixPath(path).parts
        if any(part.lower() in SKIP_DIRS for part in parts[:-1]):
            skipped.append({"path": path, "reason": "generated_or_vendor_directory"})
            continue
        if info.flag_bits & 0x1:
            skipped.append({"path": path, "reason": "encrypted"})
            continue
        if info.file_size > MAX_MEMBER_BYTES:
            skipped.append({"path": path, "reason": "member_too_large"})
            continue
        if info.file_size and info.compress_size == 0:
            skipped.append({"path": path, "reason": "suspicious_compression_ratio"})
            continue
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            skipped.append({"path": path, "reason": "suspicious_compression_ratio"})
            continue
        if len(files) >= MAX_MANIFEST_FILES:
            skipped.append({"path": path, "reason": "manifest_limit"})
            continue
        ext = PurePosixPath(path).suffix.lower()
        language = LANGUAGES.get(ext)
        digest = None
        sample = b""
        # Hash the member stream in bounded blocks. Read at most MAX_MEMBER_BYTES.
        try:
            h = hashlib.sha256()
            total = 0
            with zf.open(info, "r") as src:
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_MEMBER_BYTES:
                        raise ValueError("member_limit")
                    h.update(block)
                    if len(sample) < 256 * 1024:
                        sample += block[:256 * 1024 - len(sample)]
            digest = h.hexdigest()
        except Exception as exc:
            skipped.append({"path": path, "reason": "read_error" if str(exc) != "member_limit" else "member_too_large"})
            continue
        lower = path.lower()
        if language:
            languages.add(language)
        if PurePosixPath(path).name.lower() in ENTRY_NAMES:
            entrypoints.append(path)
        # Only inspect small, known text manifests for dependency/framework hints.
        text_lower = sample.decode("utf-8", errors="ignore").lower()
        if PurePosixPath(path).name.lower() in {"requirements.txt", "pyproject.toml", "package.json", "composer.json", "pom.xml", "cargo.toml", "go.mod"}:
            for marker in ("django", "flask", "fastapi", "react", "next", "vue", "@angular/core", "svelte", "express", "@nestjs/core", "laravel/framework", "symfony/framework-bundle", "spring-boot", "microsoft.aspnetcore", "flutter"):
                if marker in text_lower:
                    dependencies.add(marker)
        item = {"path": path, "size": info.file_size, "compressed_size": info.compress_size,
                "sha256": digest, "extension": ext, "language": language}
        if language and ext in {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".cs", ".go", ".rs", ".php", ".rb", ".vue", ".svelte"}:
            semantic = analyze_source(path, sample)
            item["semantic"] = semantic
            item["imports"] = semantic.get("imports", [])[:100]
            item["symbols"] = semantic.get("symbols", [])[:100]
            if semantic.get("status") == "syntax_error":
                item["syntax_error"] = {"line": semantic.get("line"), "message": semantic.get("message", "syntax error")}
        files.append(item)
    for framework, markers in FRAMEWORK_MARKERS.items():
        if any(marker in dependencies for marker in markers) or (framework == "Django" and any(PurePosixPath(p).name == "manage.py" for p in entrypoints)):
            frameworks.add(framework)
    # Root-level project manifests are useful evidence even if dependencies are not parsed.
    names = {PurePosixPath(f["path"]).name.lower() for f in files}
    if "package.json" in names and not frameworks.intersection({"React", "Next.js", "Vue", "Angular", "Svelte", "Express", "NestJS"}):
        frameworks.add("Node.js (package manifest detected)")
    known_paths = {item["path"] for item in files}
    python_modules = {}
    for item in files:
        if item.get("extension") == ".py":
            module = item["path"][:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[:-9]
            python_modules[module] = item["path"]
    import_graph = {}
    for item in files:
        if item.get("extension") != ".py" or not item.get("imports"):
            continue
        source_module = item["path"][:-3].replace("/", ".")
        if source_module.endswith(".__init__"):
            source_module = source_module[:-9]
        targets = []
        for imported in item["imports"]:
            candidate = imported.lstrip(".")
            while candidate:
                if candidate in python_modules:
                    targets.append(python_modules[candidate])
                    break
                candidate = candidate.rpartition(".")[0]
        if targets:
            import_graph[item["path"]] = sorted(set(targets))
    return {
        "schema_version": 1, "archive_name": str(archive_name)[:255],
        "archive_sha256": hashlib.sha256(data).hexdigest(),
        "total_entries": len(infos), "indexed_entries": len(files),
        "declared_uncompressed_bytes": declared,
        "files": files, "skipped": skipped,
        "languages": sorted(languages), "frameworks": sorted(frameworks),
        "dependencies_detected": sorted(dependencies), "entrypoints": sorted(set(entrypoints)),
        "import_graph": import_graph,
        "syntax_errors": [{"path": item["path"], **item["syntax_error"]} for item in files if item.get("syntax_error")],
        "coverage": round(len(files) / max(1, len(infos)), 4),
        "warnings": warnings,
    }


def compare_manifests(previous: dict | None, current: dict) -> dict:
    """Return path-level added/changed/removed/unchanged files based on SHA-256."""
    old = {x["path"]: x for x in (previous or {}).get("files", [])}
    new = {x["path"]: x for x in current.get("files", [])}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(p for p in set(old) & set(new) if old[p].get("sha256") != new[p].get("sha256"))
    unchanged = sorted(p for p in set(old) & set(new) if old[p].get("sha256") == new[p].get("sha256"))
    return {"added": added, "changed": changed, "removed": removed, "unchanged": unchanged,
            "requires_full_analysis": previous is None}
