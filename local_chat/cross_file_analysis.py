"""Conservative cross-file symbol/reference analysis; static and non-executing."""
from __future__ import annotations
from collections import defaultdict
from pathlib import PurePosixPath


def build_cross_file_index(manifest: dict, max_edges: int = 5000) -> dict:
    files = [x for x in manifest.get("files", []) if isinstance(x, dict) and x.get("path")]
    symbols_by_name = defaultdict(list)
    module_to_path = {}
    for item in files:
        path = str(item["path"])
        ext = str(item.get("extension") or PurePosixPath(path).suffix).lower()
        if ext == ".py":
            module = path[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[:-9]
            module_to_path[module] = path
        for sym in item.get("symbols", [])[:300]:
            if isinstance(sym, dict) and sym.get("name"):
                symbols_by_name[str(sym["name"])].append({
                    "path": path, "kind": sym.get("kind"), "line": sym.get("line"),
                    "parent": sym.get("parent")})
    import_edges, reference_edges, unresolved = [], [], []
    graph = manifest.get("import_graph") or {}
    for source, targets in graph.items():
        for target in targets if isinstance(targets, list) else []:
            if source != target:
                import_edges.append({"source": source, "target": target, "kind": "imports"})
    for item in files:
        source = str(item["path"])
        semantic = item.get("semantic") if isinstance(item.get("semantic"), dict) else {}
        calls = semantic.get("calls", [])
        for call in calls[:500]:
            raw = str(call.get("name") or "")
            leaf = raw.rsplit(".", 1)[-1]
            if not leaf or leaf in {"self", "cls"}:
                continue
            candidates = symbols_by_name.get(leaf, [])
            # Prefer references outside the current file; retain ambiguity rather than guessing.
            external = [x for x in candidates if x["path"] != source]
            if len(external) == 1:
                target = external[0]
                reference_edges.append({"source": source, "target": target["path"],
                    "symbol": leaf, "line": call.get("line"), "scope": call.get("scope"),
                    "kind": "possible_call"})
            elif len(external) > 1:
                unresolved.append({"source": source, "symbol": leaf,
                    "line": call.get("line"), "candidate_paths": sorted({x["path"] for x in external})[:12],
                    "reason": "ambiguous_symbol"})
    # Deduplicate and cap output to keep manifests bounded.
    def dedupe(rows, keys):
        seen, out = set(), []
        for row in rows:
            key = tuple(row.get(k) for k in keys)
            if key not in seen:
                seen.add(key); out.append(row)
        return out[:max_edges]
    import_edges = dedupe(import_edges, ("source", "target", "kind"))
    reference_edges = dedupe(reference_edges, ("source", "target", "symbol", "line"))
    return {
        "schema_version": 1,
        "symbol_count": sum(len(v) for v in symbols_by_name.values()),
        "unique_symbol_names": len(symbols_by_name),
        "import_edges": import_edges,
        "possible_call_edges": reference_edges,
        "ambiguous_references": unresolved[:max_edges],
        "limitations": ["ارجاع‌های فراخوانی احتمالی‌اند و حل نوع/dispatch پویا انجام نمی‌شود.",
                        "نام‌های مبهم عمداً به یک فایل خاص نسبت داده نمی‌شوند."],
    }
