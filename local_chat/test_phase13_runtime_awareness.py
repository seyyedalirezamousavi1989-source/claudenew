from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent


class Phase13RuntimeAwarenessTests(unittest.TestCase):
    def test_search_autorouting_covers_explicit_persian_and_english_requests(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("توی|در", source)
        self.assertIn("تحقیق", source)
        self.assertIn("research", source)

    def test_audio_fallback_requires_a_persian_system_voice(self):
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("playBrowserPersianSpeech", source)
        self.assertIn("/^fa(?:-|$)/i", source)
        self.assertIn("window.speechSynthesis?.cancel()", source)

    def test_server_time_endpoint_and_local_voice_asset_fallback_exist(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn('path == "/api/time"', source)
        self.assertIn("_parsvoice_assets_ready", source)
        self.assertIn("local_assets_ready", source)


if __name__ == "__main__":
    unittest.main()
