"""Phase 9 regressions: default local-runtime identity and per-turn search isolation."""
from unittest.mock import patch

from local_chat import server as sv


def test_system_prompt_knows_creator_and_local_ollama_runtime_by_default():
    with patch.object(sv, "_installed_models", return_value=["qwen3-coder:30b", "deepseek-r1:32b"]):
        sv._LOCAL_MODELS_CONTEXT_CACHE.update(expires=0.0, names=None)
        prompt = sv._system_prompt(model="qwen3-coder:30b")
    assert "LocalChat" in prompt
    assert sv.CREATOR["name"] in prompt
    assert "به‌صورت محلی" in prompt
    assert "Ollama" in prompt
    assert "qwen3-coder:30b" in prompt
    assert "deepseek-r1:32b" in prompt
    assert "مدل انتخاب‌شده" in prompt


def test_local_environment_does_not_guess_inventory_when_ollama_unavailable():
    with patch.object(sv, "_installed_models", return_value=None):
        sv._LOCAL_MODELS_CONTEXT_CACHE.update(expires=0.0, names=None)
        facts = sv._local_environment_facts()
    assert "فهرست لحظه‌ای مدل‌های نصب‌شده فعلاً قابل‌دریافت نیست" in facts
    assert "مدل‌های محلی که اکنون Ollama گزارش می‌کند" not in facts


def test_search_request_id_is_validated_as_bounded_opaque_token():
    import re
    valid = "turn_12345678-abc"
    assert re.fullmatch(r"[A-Za-z0-9_-]{8,80}", valid)
    assert not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", "short")
    assert not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", "x" * 81)


def test_frontend_binds_search_sources_to_exact_user_turn():
    from pathlib import Path
    js = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "request_id: requestId" in js
    assert "data.request_id !== requestId || data.requested_query !== query" in js
    assert "chat.messages.includes(userMessage)" in js
    assert "عبارت دقیق این جست‌وجو" in js
    assert "فقط همین عبارت و منابع پیوست‌شده به همین پیام" in js
