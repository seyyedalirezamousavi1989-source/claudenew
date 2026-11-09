"""Bounded, read-only semantic indexing for source files in uploaded archives.

This module parses Python with the standard-library AST and extracts conservative
symbol/import/call/route metadata from JS/TS and common compiled-language sources.
It never imports or executes project code. Results are metadata only.
"""
from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

MAX_SOURCE_BYTES = 256 * 1024
MAX_SYMBOLS = 300
MAX_CALLS = 500
MAX_IMPORTS = 200


def _decode(source: bytes) -> str:
    return source.decode("utf-8-sig", errors="replace")


class _PythonIndex(ast.NodeVisitor):
    def __init__(self):
        self.symbols = []
        self.calls = []
        self.imports = set()
        self.routes = []
        self._scope = []

    def _add_symbol(self, kind, node, parent=None):
        item = {"kind": kind, "name": node.name, "line": getattr(node, "lineno", None),
                "end_line": getattr(node, "end_lineno", None), "parent": parent,
                "doc": ast.get_docstring(node, clean=True)[:240] if ast.get_docstring(node) else ""}
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            item["async"] = isinstance(node, ast.AsyncFunctionDef)
            item["decorators"] = [ast.unparse(x)[:120] for x in node.decorator_list[:8]]
            item["parameters"] = [a.arg for a in (node.args.posonlyargs + node.args.args + node.args.kwonlyargs)[:30]]
            item["returns"] = ast.unparse(node.returns)[:120] if node.returns else None
        self.symbols.append(item)

    def visit_Import(self, node):
        self.imports.update(alias.name for alias in node.names)

    def visit_ImportFrom(self, node):
        prefix = "." * (node.level or 0) + (node.module or "")
        self.imports.add(prefix)

    def visit_ClassDef(self, node):
        parent = self._scope[-1] if self._scope else None
        self._add_symbol("class", node, parent)
        self._scope.append(node.name)
        for base in node.bases:
            self.visit(base)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for child in node.body:
            self.visit(child)
        self._scope.pop()

    def visit_FunctionDef(self, node):
        self._visit_function(node, "function")

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node, "function")

    def _visit_function(self, node, kind):
        parent = self._scope[-1] if self._scope else None
        self._add_symbol(kind, node, parent)
        self._scope.append(node.name)
        for child in node.body:
            self.visit(child)
        self._scope.pop()

    def visit_Call(self, node):
        try:
            name = ast.unparse(node.func)[:180]
        except Exception:
            name = type(node.func).__name__
        self.calls.append({"name": name, "line": getattr(node, "lineno", None),
                           "scope": ".".join(self._scope) or "<module>"})
        # Django/Flask/FastAPI-style route decorators are captured separately.
        self.generic_visit(node)


def analyze_source(path: str, source: bytes) -> dict:
    """Return bounded semantic metadata; unsupported/oversized files are explicit."""
    ext = PurePosixPath(path).suffix.lower()
    if len(source) > MAX_SOURCE_BYTES:
        return {"status": "skipped", "reason": "source_too_large", "bytes": len(source)}
    text = _decode(source)
    if ext == ".py":
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError as exc:
            return {"status": "syntax_error", "line": exc.lineno, "message": str(exc.msg)[:160],
                    "symbols": [], "imports": [], "calls": [], "routes": []}
        index = _PythonIndex()
        index.visit(tree)
        routes = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                try:
                    value = ast.unparse(dec)
                except Exception:
                    continue
                if any(token in value.lower() for token in (".route(", ".get(", ".post(", ".put(", ".delete(", ".patch(")):
                    routes.append({"function": node.name, "decorator": value[:180], "line": node.lineno})
        return {"status": "ok", "parser": "python_ast", "symbols": index.symbols[:MAX_SYMBOLS],
                "imports": sorted(index.imports)[:MAX_IMPORTS], "calls": index.calls[:MAX_CALLS],
                "routes": routes[:100], "symbol_count": len(index.symbols),
                "call_count": len(index.calls)}

    # Conservative lexical extraction: no attempt to claim type-resolved semantics.
    symbols, imports, routes = [], set(), []
    patterns = [
        ("class", r"\bclass\s+([A-Za-z_$][\w$]*)"),
        ("function", r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\("),
        ("function", r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*=>"),
        ("function", r"\b(?:export\s+)?(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^;{}]*\)\s*\{"),
    ]
    if ext in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"}:
        for match in re.finditer(r"\bimport\s+(?:[^'\"]+?\s+from\s+)?['\"]([^'\"]+)['\"]|\brequire\s*\(\s*['\"]([^'\"]+)['\"]", text):
            imports.add(match.group(1) or match.group(2))
        for kind, pattern in patterns:
            for match in re.finditer(pattern, text):
                symbols.append({"kind": kind, "name": match.group(1), "line": text.count("\n", 0, match.start()) + 1})
        for match in re.finditer(r"\.(get|post|put|patch|delete)\s*\(\s*['\"]([^'\"]+)", text, re.I):
            routes.append({"method": match.group(1).upper(), "path": match.group(2),
                           "line": text.count("\n", 0, match.start()) + 1})
    elif ext in {".java", ".kt", ".cs", ".go", ".rs", ".php", ".rb"}:
        for match in re.finditer(r"^\s*import\s+([\w.*\\/]+)", text, re.M):
            imports.add(match.group(1))
        for kind, pattern in (("class", r"\bclass\s+([A-Za-z_]\w*)"),
                              ("function", r"\b(?:def|func|fn|void|public|private|protected)\s+([A-Za-z_]\w*)\s*\(")):
            for match in re.finditer(pattern, text):
                symbols.append({"kind": kind, "name": match.group(1),
                                "line": text.count("\n", 0, match.start()) + 1})
    return {"status": "ok" if symbols or imports else "partial", "parser": "bounded_lexical",
            "symbols": symbols[:MAX_SYMBOLS], "imports": sorted(imports)[:MAX_IMPORTS],
            "calls": [], "routes": routes[:100], "symbol_count": len(symbols),
            "call_count": 0, "limitations": ["تحلیل واژگانی است؛ حل نوع و ارجاع دقیق انجام نشده است."]}
