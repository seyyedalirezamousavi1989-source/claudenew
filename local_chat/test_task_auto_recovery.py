"""Safety tests for automatic restart recovery of persisted document tasks."""
import tempfile
import unittest
from pathlib import Path

from local_chat import task_service, tasks


class AutoRecoveryTests(unittest.TestCase):
    def _interrupted_task(self, root):
        manager = tasks.TaskManager(root)
        task_id = manager.create("summarize", "source.txt", b"A short document for recovery testing.\n" * 30,
                                 {"model": "test-model:latest", "target": "fa", "num_ctx": 4096,
                                  "num_predict": 512, "max_tokens": 300, "workers": 1})
        manager.prepare(task_id)
        manager._set(task_id, status=tasks.INTERRUPTED, stage="interrupted", owner="", run_started=None)
        return task_id

    def test_missing_model_leaves_task_interrupted(self):
        with tempfile.TemporaryDirectory() as root:
            task_id = self._interrupted_task(root)
            service = task_service.TaskService(lambda: root, lambda *a, **k: {}, lambda m: 4096,
                                               model_available=lambda model: False)
            manager = service.manager
            self.assertEqual(manager.get(task_id)["status"], tasks.INTERRUPTED)
            self.assertNotIn(task_id, manager._runners)

    def test_installed_model_starts_only_interrupted_task(self):
        with tempfile.TemporaryDirectory() as root:
            task_id = self._interrupted_task(root)
            service = task_service.TaskService(lambda: root, lambda *a, **k: {"message": {"content": "summary"}},
                                               lambda m: 4096, model_available=lambda model: model == "test-model:latest")
            manager = service.manager
            self.assertIn(task_id, manager._runners)
            self.assertEqual(manager.get(task_id)["status"], tasks.PREPARING)
            service.stop_all(2)


if __name__ == "__main__":
    unittest.main()
