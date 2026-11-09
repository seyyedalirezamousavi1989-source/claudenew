"""Query-aware view of an uploaded project archive that fits a small model context.

``files.extract_zip`` produces one long text: a file tree followed by ``=== path ===`` sections.
Cutting that text at the context limit (what used to happen) leaves the model with the tree and the first
few shallow files only, so it cannot analyse the project. This module builds, within a character budget:

1. the archive header and file tree (clipped),
2. a *code map*: the signatures (classes, functions, routes, exports) of EVERY file, so the whole project is visible,
3. full excerpts of the files that matter for the question (entry points, README, files whose path or
   content matches the question - Persian questions are bridged to English identifiers).
"""
import math
import re

SECTION = re.compile(r"^=== (.+?)(?: \[[^\]\n]*\])? ===[ \t]*\n", re.M)

_STOP = frozenset((
    "را", "که", "این", "آن", "اون", "با", "از", "در", "به", "هم", "یا", "برای", "چی", "چیه", "چطور", "چرا", "کن", "کنید", "بکن",
    "the", "and", "for", "what", "how", "why", "this", "that", "with", "please", "project", "file", "files", "code",
    "پروژه", "فایل", "فایل‌ها", "فایلها", "کد", "لطفا", "لطفاً", "خوب", "یه", "یک", "هست", "است", "میکنه", "می‌کنه", "کار",
))

# Persian concepts -> identifiers that appear in code. Cheap and effective: a Persian question about an English
# code base shares no literal words with it, which is why lexical retrieval used to return nothing.
CONCEPTS = (
    (r"پایگاه|دیتابیس|داده‌?ها|ذخیره|database|storage", ("db", "database", "sql", "sqlite", "store", "model", "schema")),
    (r"ورود|لاگین|احراز|هویت|رمز|auth|login", ("auth", "login", "session", "token", "identity", "password", "csrf")),
    (r"تست|آزمون|test", ("test", "spec", "assert")),
    (r"جست[‌\s]?و[‌\s]?جو|سرچ|اینترنت|وب|search|web", ("search", "web", "fetch", "provider", "engine", "query")),
    (r"ترجمه|translat", ("translate", "translation", "pipeline", "chunk", "task")),
    (r"خلاصه|summar", ("summarize", "summary", "pipeline", "task")),
    (r"صدا|صوت|گفتار|میکروفون|voice|audio|speech|tts|stt", ("audio", "voice", "tts", "stt", "speech", "whisper")),
    (r"تنظیم|پیکربندی|config|setting", ("config", "settings", "options", "env")),
    (r"رابط|ظاهر|صفحه|دکمه|ui|frontend|html|css", ("ui", "html", "css", "static", "app", "index", "render")),
    (r"سرور|مسیر|api|route|endpoint|handler|درخواست", ("server", "route", "router", "handler", "endpoint", "api", "request")),
    (r"فارکس|بازار|نمودار|forex|chart|mt5", ("forex", "chart", "mt5", "candle", "market")),
    (r"حافظه|memory", ("memory", "remember", "store")),
    (r"نصب|بیلد|ساخت|بسته|install|build|package", ("build", "install", "setup", "spec", "installer", "package", "bat")),
    (r"مدل|ollama|model", ("model", "ollama", "llm", "generate", "chat")),
    (r"فایل|پیوست|آپلود|attach|upload|file", ("file", "files", "attachment", "upload", "extract", "zip")),
    (r"خطا|باگ|مشکل|اشکال|ایراد|bug|error|issue|fix", ("error", "exception", "raise", "try", "except", "fail", "todo", "fixme")),
    (r"امنیت|security|safe", ("security", "sanitize", "escape", "csrf", "ssrf", "permission", "validate")),
)

# "tell me about the whole project": no keyword focus, maximise coverage instead
OVERVIEW = re.compile(
    r"(تحلیل|بررسی|توضیح|معرفی|خلاصه|ساختار|معماری|کلی|مرور|چیکار|چه\s*کار|چطور\s*کار|ایراد|مشکل|باگ|بهبود|ارزیابی|"
    r"review|analy[sz]e|overview|summar|explain|structure|architecture|audit|improve|bug|issue|what\s+does)", re.I)

IMPORTANT = (
    (re.compile(r"(^|/)(readme|about)[^/]*$", re.I), 6),
    (re.compile(r"(^|/)(main|app|server|index|manage|wsgi|asgi|cli|__main__|launcher|start)\.[a-z]+$", re.I), 6),
    (re.compile(r"(^|/)(package\.json|pyproject\.toml|requirements[^/]*\.txt|setup\.py|pubspec\.yaml|build\.gradle(\.kts)?|"
                r"cargo\.toml|go\.mod|pom\.xml|dockerfile|docker-compose[^/]*|makefile)$", re.I), 4),
    (re.compile(r"(^|/)(settings|config|version)[^/]*\.(py|json|ya?ml|toml|js|ts)$", re.I), 3),
)

SIGNATURE = re.compile(
    r"^[ \t]*(?:(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:def|class|function|interface|enum|struct|trait|impl|fn|func|"
    r"module|namespace|type)\b|(?:public|private|protected|static|abstract)\b[^=;]*\(|@[\w.]+(?:\(.*)?$|"
    r"(?:app|router|bp|api)\.(?:get|post|put|patch|delete|route|use)\(|(?:export\s+)?const\s+\w+\s*=\s*(?:async\s*)?\(?[\w, ]*\)?\s*=>)",
    re.M)
IMPORT = re.compile(r"^[ \t]*(?:import\s+\S|from\s+\S+\s+import\b|const\s+\S+\s*=\s*require\()", re.M)


def split_identifiers(value):
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
    return [w.casefold() for w in re.findall(r"[A-Za-z0-9؀-ۿ]+", value.replace("_", " ").replace("-", " ").replace(".", " "))]


def query_terms(query):
    """(terms, overview) - words to look for in paths/code, expanded from Persian concepts."""
    text = str(query or "")
    terms = {w for w in split_identifiers(text) if len(w) > 2 and w not in _STOP}
    for pattern, words in CONCEPTS:
        if re.search(pattern, text, re.I):
            terms.update(words)
    return terms, bool(OVERVIEW.search(text))


def split_archive(text):
    """(header, [(path, body), ...]) from the ``=== path ===`` layout produced by files.extract_zip."""
    matches = list(SECTION.finditer(text))
    if not matches:
        return text, []
    header = text[:matches[0].start()]
    sections = []
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        sections.append((match.group(1).strip(), text[match.end():end].rstrip()))
    return header, sections


def _importance(path):
    return max((weight for pattern, weight in IMPORTANT if pattern.search(path)), default=0)


def _is_test(path):
    return bool(re.search(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]*$|[._-](test|spec)\.[a-z]+$", path, re.I))


def score_file(path, body, terms):
    path_terms = set(split_identifiers(path))
    score = float(_importance(path))
    if terms:
        score += 3.0 * len(terms & path_terms)
        lowered = body.casefold()
        hits = [lowered.count(t) for t in terms if len(t) > 2]
        score += sum(math.log1p(h) for h in hits if h) * 1.2
    if _is_test(path) and not (terms & {"test", "spec", "assert"}):
        score -= 2.0
    return score


def skeleton(body, max_lines):
    """Imports (condensed) and signatures of one file - its 'table of contents'."""
    lines = body.split("\n")
    imports = [ln.strip() for ln in lines if IMPORT.match(ln)][:6]
    signatures = []
    for number, line in enumerate(lines, 1):
        if SIGNATURE.match(line):
            signatures.append(f"{number}: {line.strip()[:130]}")
            if len(signatures) >= max_lines:
                break
    out = []
    if imports:
        out.append("imports: " + "; ".join(i[:70] for i in imports)[:260])
    out.extend(signatures)
    if not signatures:
        # docs, configs, templates: no code signatures - show what the file starts with so it is still on the map
        heads = [ln.strip()[:120] for ln in lines if ln.strip()][:2]
        out.extend(f"» {h}" for h in heads)
    return out


def _best_windows(body, terms, size=45, limit_chars=2500):
    lines = body.split("\n")
    if len("\n".join(lines)) <= limit_chars:
        return [(1, len(lines), "\n".join(lines))]
    windows = []
    for start in range(0, len(lines), size // 2):
        chunk = lines[start:start + size]
        text = "\n".join(chunk)
        lowered = text.casefold()
        hit = sum(1 for t in terms if t in lowered) if terms else 0
        windows.append((hit, -start, start, text))
    if not terms or not any(w[0] for w in windows):
        first = "\n".join(lines[:size])
        return [(1, min(len(lines), size), first[:limit_chars])]
    windows.sort(reverse=True)
    chosen, used, taken = [], 0, set()
    for hit, _neg, start, text in windows:
        if not hit or used >= limit_chars:
            break
        if any(abs(start - other) < size // 2 for other in taken):
            continue
        taken.add(start)
        chosen.append((start + 1, start + len(text.split("\n")), text[:limit_chars - used]))
        used += len(text)
    chosen.sort()
    return chosen


def build_digest(text, query="", max_chars=12000, extra=""):
    """Fit an archive text into ``max_chars`` while keeping the whole project visible. Returns the digest text."""
    text = str(text or "")
    max_chars = max(1500, int(max_chars))
    header, sections = split_archive(text)
    if not sections:
        return text[:max_chars]
    terms, overview = query_terms(query)
    scored = sorted(((score_file(p, b, terms), p, b) for p, b in sections), key=lambda x: (-x[0], x[1]))

    parts = []
    header = header.strip()
    tree_budget = int(max_chars * 0.18)
    parts.append(header if len(header) <= tree_budget else header[:tree_budget] + "\n  … (ادامهٔ فهرست کوتاه شد)")
    if extra:
        parts.append(extra.strip()[:int(max_chars * 0.14)])

    # --- code map: every file, ordered by relevance, a few signatures each -----------------------------
    map_budget = int(max_chars * (0.42 if overview or not terms else 0.28))
    per_file = max(2, min(14, map_budget // max(1, len(scored) * 70)))
    map_lines, used = ["", "نقشهٔ کد (واردکردنی‌ها و امضاهای هر فایل؛ شمارهٔ خط در ابتدا):"], 0
    for _score, path, body in scored:
        lines = skeleton(body, per_file)
        if not lines:
            continue
        block = f"[{path}] {len(body.splitlines())} خط\n  " + "\n  ".join(lines)
        if used + len(block) > map_budget:
            break
        map_lines.append(block)
        used += len(block)
    parts.append("\n".join(map_lines))

    # --- excerpts of the files that matter most ----------------------------------------------------------
    remaining = max_chars - sum(len(p) for p in parts) - 200
    excerpts = ["", "بخش‌های مهم فایل‌ها (متن واقعی):"]
    per_excerpt = max(900, min(4500, remaining // 4)) if remaining > 0 else 0
    for score, path, body in scored:
        if remaining < 700 or (terms and score <= 0 and not overview):
            break
        windows = _best_windows(body, terms, limit_chars=min(per_excerpt, remaining - 120))
        for first, last, chunk in windows:
            block = f"\n=== {path} (خط {first}–{last}) ===\n{chunk}"
            if len(block) > remaining:
                break
            excerpts.append(block)
            remaining -= len(block)
    if len(excerpts) > 2:
        parts.append("\n".join(excerpts))
    parts.append("\n[این نمایش برای جا شدن در زمینهٔ مدل فشرده شده است: نقشهٔ همهٔ فایل‌ها + متن کامل فایل‌های مرتبط. "
                 "اگر بخش دیگری لازم بود، نام فایل را در پرسش بنویس.]")
    return "\n".join(parts)[:max_chars + 400]
