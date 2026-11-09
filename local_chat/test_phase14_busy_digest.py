"""Phase 14: one heavy job at a time, live task progress, query-aware project digest, focused web text."""
import json
import re
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

from local_chat import project_digest, search_engine
from local_chat import server as srv
from local_chat.test_docengine import make_book
from local_chat.test_features import FeatureTestCase


class BusyGateTests(FeatureTestCase):
    def setUp(self):
        super().setUp()
        self.release = threading.Event()

        def ollama_post(path, payload, timeout=120):
            self.release.wait(20)                          # keeps the first chunk "running" until the test lets go
            body = payload["messages"][-1]["content"].split("متن برای ترجمه:\n\n", 1)[1]
            return {"message": {"content": "ترجمه: " + body + "\n<<TERMS>>\nGPU = پردازنده"}}

        self.patches2 = [
            patch.object(srv, "_ollama_post", ollama_post),
            patch.object(srv, "APP_DATA_DIR", srv.Path(self.tmp.name)),
            patch.object(srv, "TASK_SERVICE", None),
            patch.object(srv, "_model_context_capacity", lambda model: 16384),
            patch.object(srv, "_model_capabilities", lambda model: ["completion"]),
        ]
        for item in self.patches2:
            item.start()

    def tearDown(self):
        self.release.set()
        service = srv.TASK_SERVICE
        if service is not None:
            service.stop_all(5)
        for item in reversed(self.patches2):
            item.stop()
        super().tearDown()

    def create_task(self, name="book.txt"):
        data = make_book(chapters=6, sections=2, paragraphs=4).encode()
        options = {"kind": "translate", "model": "text:8b", "target": "fa"}
        return self.call("POST", "/api/tasks", raw=data, headers={
            "X-Filename": quote(name), "X-Task-Options": quote(json.dumps(options)),
            "Content-Type": "application/octet-stream"})

    def wait_for(self, predicate, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            _, body = self.call("GET", "/api/tasks")
            data = json.loads(body)
            if predicate(data):
                return data
            time.sleep(0.05)
        self.fail("condition not reached")

    def test_running_translation_blocks_second_task_and_chat_and_reports_live_progress(self):
        status, body = self.create_task()
        self.assertEqual(status, 201, body)
        task_id = json.loads(body)["task"]["id"]
        data = self.wait_for(lambda d: d["tasks"] and d["tasks"][0]["current_chunk"])
        self.assertTrue(data["busy"]["busy"])
        self.assertEqual(data["busy"]["kind"], "task")
        task = data["tasks"][0]
        self.assertTrue(task["active"])
        self.assertEqual(task["current_chunk"], 1)
        self.assertIsNotNone(task["current_chunk_seconds"])

        status, body = self.create_task("second.txt")                  # a second long job is refused with a reason
        self.assertEqual(status, 409)
        self.assertIn("در حال", json.loads(body)["error"])

        status, body = self.call("POST", "/api/chat", {"model": "text:8b", "messages": [{"role": "user", "content": "سلام"}]})
        self.assertEqual(status, 409)
        payload = json.loads(body)
        self.assertEqual(payload["code"], "busy")
        self.assertIn("پیام جدید", payload["error"])

        self.call("POST", f"/api/tasks/{task_id}/pause", {})           # pausing frees the model for chat again
        self.release.set()
        self.wait_for(lambda d: not d["busy"]["busy"])
        slot = srv._acquire_model_slot()
        self.assertFalse(isinstance(slot, str), slot)
        slot.release()

    def test_chat_slot_refuses_a_second_concurrent_reply(self):
        slot = srv._acquire_model_slot()
        self.assertFalse(isinstance(slot, str))
        try:
            second = srv._acquire_model_slot()
            self.assertIsInstance(second, str)                          # reason, not a lock
            status, body = self.call("POST", "/api/chat", {"model": "text:8b", "messages": [{"role": "user", "content": "سلام"}]})
            self.assertEqual(status, 409)
            self.assertEqual(json.loads(body)["code"], "busy")
            self.assertEqual(srv._busy_status()["kind"], "chat")
        finally:
            slot.release()
        self.assertFalse(srv._busy_status()["busy"])


SAMPLE_ARCHIVE = "\n".join([
    "آرشیو ZIP «demo.zip»: 4 فایل", "", "ساختار:", "  app/server.py (1 KB)", "  app/payments.py (1 KB)", "  README.md (1 KB)", "",
    "محتوای فایل‌های متنی:",
    "\n=== README.md ===\n# Demo shop\nA tiny shop API.",
    "\n=== app/server.py ===\nimport http.server\n\nclass Handler:\n    def do_GET(self):\n        return 'ok'\n\n@app.get('/items')\ndef list_items():\n    return []\n",
    "\n=== app/payments.py ===\nimport stripe\n\ndef charge_card(user, amount):\n    # TODO validate amount\n    return stripe.Charge.create(amount=amount)\n\n"
    + "\n".join(f"def filler_{i}():\n    return {i}" for i in range(400)),
    "\n=== app/auth.py ===\ndef login(user, password):\n    return check_password(user, password)\n",
])


class ProjectDigestTests(unittest.TestCase):
    def test_every_file_is_visible_in_the_code_map_within_budget(self):
        digest = project_digest.build_digest(SAMPLE_ARCHIVE, "این پروژه چیکار می‌کنه؟ تحلیل کن", 6000)
        self.assertLessEqual(len(digest), 6500)
        for path in ("README.md", "app/server.py", "app/payments.py", "app/auth.py"):
            self.assertIn(f"[{path}]", digest)
        self.assertIn("def charge_card", digest)
        self.assertIn("list_items", digest)

    def test_persian_question_about_english_code_finds_the_relevant_file(self):
        digest = project_digest.build_digest(SAMPLE_ARCHIVE, "ورود کاربر و رمز عبور چطور بررسی می‌شود؟", 5000)
        self.assertIn("=== app/auth.py", digest)
        self.assertIn("check_password", digest)

    def test_short_archives_are_not_changed_by_render_for_prompt(self):
        from local_chat import files
        record = {"name": "demo.zip", "kind": "archive", "size": 10, "text": SAMPLE_ARCHIVE, "digest_query": "تحلیل"}
        block, clipped = files.render_for_prompt(record, 200000)
        self.assertFalse(clipped)
        self.assertIn("filler_399", block)
        block, clipped = files.render_for_prompt(record, 4000)
        self.assertTrue(clipped)
        self.assertIn("نقشهٔ کد", block)

    def test_zip_reader_reads_source_code_before_root_notes(self):
        from local_chat import files
        self.assertLess(files._read_priority("local_chat/server.py"), files._read_priority("NOTES_FA.md"))
        self.assertLess(files._read_priority("README.md"), files._read_priority("src/a.py"))
        self.assertLess(files._read_priority("src/a.py"), files._read_priority("tests/test_a.py"))


class FocusedWebTextTests(unittest.TestCase):
    def test_relevant_passage_is_kept_instead_of_page_head(self):
        junk = "منوی اصلی ورود ثبت نام تماس با ما. " * 120
        page = junk + "قیمت RTX 4070 در ایران حدود ۳۵ میلیون تومان است. " + "متن نامرتبط دربارهٔ هوا. " * 120
        focused = search_engine.focus_text(page, "قیمت RTX 4070 را سرچ کن", 600)
        self.assertIn("۳۵ میلیون", focused)
        self.assertLessEqual(len(focused), 700)

    def test_short_or_queryless_text_is_returned_as_is_or_head(self):
        self.assertEqual(search_engine.focus_text("کوتاه", "x"), "کوتاه")
        self.assertTrue(search_engine.focus_text("الف " * 2000, "", 300).startswith("الف"))


if __name__ == "__main__":
    unittest.main()
