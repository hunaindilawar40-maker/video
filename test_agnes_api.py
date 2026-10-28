"""Offline unit tests for agnes_api — no network, no key, no credits.

Run with:  python3 -m unittest -v
"""

import os
import tempfile
import unittest

import agnes_api as api


class PayloadDefaultsTest(unittest.TestCase):
    def test_1_default_payload_shape(self):
        payload = api.build_payload(prompt="a silver sports car in the rain")
        self.assertEqual(payload["model"], "agnes-video-2.5-flash")
        self.assertEqual(payload["mode"], "text")
        self.assertEqual(payload["seconds"], "5")
        self.assertEqual(payload["size"], "720P")
        self.assertEqual(payload["aspect_ratio"], "16:9")
        self.assertEqual(payload["prompt"], "a silver sports car in the rain")
        for field in ("first_frame", "last_frame", "images", "audios", "videos", "seed", "n"):
            self.assertNotIn(field, payload)

    def test_2_seconds_is_a_string_and_seed_passes_through(self):
        for value in (4, "4", 12, "12", 7.0):
            payload = api.build_payload(prompt="x", seconds=value)
            self.assertIsInstance(payload["seconds"], str)
            self.assertEqual(payload["seconds"], str(int(float(value))))
        self.assertEqual(api.build_payload(prompt="x", seed=42)["seed"], 42)
        self.assertEqual(api.build_payload(prompt="x", seed="7")["seed"], 7)
        self.assertNotIn("seed", api.build_payload(prompt="x", seed=None))

    def test_3_full_model_accepts_all_sizes_and_aspects(self):
        for size in ("720P", "1080P", "1K", "2K"):
            payload = api.build_payload(prompt="x", model="agnes-video-2.5", size=size)
            self.assertEqual(payload["size"], size)
        for aspect in api.ASPECT_RATIOS:
            payload = api.build_payload(prompt="x", model="agnes-video-2.5",
                                        aspect_ratio=aspect)
            self.assertEqual(payload["aspect_ratio"], aspect)


class ValidationTest(unittest.TestCase):
    def test_4_flash_with_1080p_is_rejected_locally(self):
        with self.assertRaises(api.AgnesValidationError) as ctx:
            api.build_payload(prompt="x", model="agnes-video-2.5-flash", size="1080P")
        self.assertIn("720P", str(ctx.exception))

    def test_5_duration_bounds(self):
        for bad in (3, 13, "0", "abc", 3.5, True):
            with self.assertRaises(api.AgnesValidationError):
                api.build_payload(prompt="x", seconds=bad)

    def test_6_empty_prompt_rejected(self):
        for bad in ("", "   ", None):
            with self.assertRaises(api.AgnesValidationError):
                api.build_payload(prompt=bad)

    def test_7_keyframe_needs_a_frame(self):
        with self.assertRaises(api.AgnesValidationError):
            api.build_payload(prompt="x", mode="keyframe")
        payload = api.build_payload(prompt="x", mode="keyframe",
                                    first_frame="https://example.com/first.png")
        self.assertEqual(payload["first_frame"], "https://example.com/first.png")
        self.assertNotIn("last_frame", payload)

    def test_8_text_mode_never_sends_media(self):
        payload = api.build_payload(prompt="x", mode="text")
        for field in api.MEDIA_FIELDS:
            self.assertNotIn(field, payload)
        with self.assertRaises(api.AgnesValidationError):
            api.build_payload(prompt="x", mode="text", images=["https://e.com/a.png"])
        with self.assertRaises(api.AgnesValidationError):
            api.build_payload(prompt="x", mode="text", first_frame="https://e.com/a.png")

    def test_9_reference_mode_rules(self):
        with self.assertRaises(api.AgnesValidationError):
            api.build_payload(prompt="x", mode="reference")
        with self.assertRaises(api.AgnesValidationError):
            api.build_payload(prompt="x", mode="reference",
                              images=["https://e.com/a.png"],
                              first_frame="https://e.com/f.png")
        with self.assertRaises(api.AgnesValidationError):
            api.build_payload(prompt="x", mode="keyframe",
                              first_frame="https://e.com/f.png",
                              audios=["https://e.com/a.mp3"])

    def test_10_reference_sorting_by_extension(self):
        refs = ["https://e.com/b.mp3", "https://e.com/a.png", "https://e.com/c.mp4",
                "https://e.com/d.webp?x=1"]
        payload = api.build_payload(prompt="use <Picture 1>", mode="reference",
                                    references=refs, model="agnes-video-2.5")
        self.assertEqual(payload["images"], ["https://e.com/a.png",
                                             "https://e.com/d.webp?x=1"])
        self.assertEqual(payload["audios"], ["https://e.com/b.mp3"])
        self.assertEqual(payload["videos"], [{"url": "https://e.com/c.mp4"}])
        self.assertNotIn("first_frame", payload)
        self.assertNotIn("last_frame", payload)

    def test_11_flash_reference_limits(self):
        six = ["https://e.com/%d.png" % i for i in range(6)]
        with self.assertRaises(api.AgnesValidationError):
            api.build_payload(prompt="x", mode="reference", images=six)
        with self.assertRaises(api.AgnesValidationError):
            api.build_payload(prompt="x", mode="reference",
                              videos=["https://e.com/a.mp4"])

    def test_12_bad_media_urls_rejected(self):
        with self.assertRaises(api.AgnesValidationError):
            api.build_payload(prompt="x", mode="keyframe", first_frame="not-a-url")
        with self.assertRaises(api.AgnesValidationError):
            api.build_payload(prompt="x", mode="reference", images=["javascript:alert(1)"])

    def test_13_bad_aspect_ratio_and_mode(self):
        with self.assertRaises(api.AgnesValidationError):
            api.build_payload(prompt="x", aspect_ratio="auto")
        with self.assertRaises(api.AgnesValidationError):
            api.build_payload(prompt="x", aspect_ratio="1280x720")
        with self.assertRaises(api.AgnesValidationError):
            api.build_payload(prompt="x", mode="magic")


class TaskIdsTest(unittest.TestCase):
    def test_14_task_id_extraction_priority(self):
        response = {"id": "task_1", "task_id": "task_1", "video_id": "video_1",
                    "model": "agnes-video-2.5-flash"}
        ids = api.task_ids(response)
        self.assertEqual(ids["video_id"], "video_1")
        self.assertEqual(ids["task_id"], "task_1")
        self.assertEqual(ids["id"], "task_1")
        self.assertEqual(ids["model"], "agnes-video-2.5-flash")

        # video_id wins even when "id" appears earlier and higher in the tree.
        nested = {"id": "task_9", "data": {"video": {"video_id": "video_9"}}}
        self.assertEqual(api.task_ids(nested)["video_id"], "video_9")
        # Falls back to id/task_id, and never returns blank strings.
        self.assertEqual(api.task_ids({"id": "task_only"})["video_id"], "task_only")
        self.assertEqual(api.task_ids({"video_id": "  ", "task_id": "task_2"})["video_id"],
                         "task_2")

class SnapshotTest(unittest.TestCase):
    def test_15_snapshot_nested_percent_and_failure_parsing(self):
        nested = {
            "code": 0,
            "data": {"task": {"status": "PROCESSING", "progress": "37%",
                              "output": {"video_url": "https://cdn.example.com/a.mp4"}}},
        }
        snap = api.snapshot(nested)
        self.assertEqual(snap["progress"], 37)
        self.assertEqual(snap["status"], "processing")
        self.assertEqual(snap["video_url"], "https://cdn.example.com/a.mp4")
        self.assertIsNone(snap["error"])

        done = api.snapshot({"result": {"state": "succeeded",
                                        "download_url": "https://cdn.example.com/b.mp4"}})
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["video_url"], "https://cdn.example.com/b.mp4")

        failed = api.snapshot({"video_id": "video_1", "status": "failed",
                               "progress": 100,
                               "error": {"message": "Invalid reference media"}})
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "Invalid reference media")
        self.assertIsNone(failed["video_url"])

        # Non-URL values are never accepted as video_url.
        bogus = api.snapshot({"status": "completed", "url": "not-a-url"})
        self.assertIsNone(bogus["video_url"])

        # progress given as a 0..1 float, and flat percent strings.
        self.assertEqual(api.snapshot({"progress": 0.5})["progress"], 50)
        self.assertEqual(api.snapshot({"progress": "80"})["progress"], 80)
        # Unknown status with a URL implies completion.
        self.assertEqual(api.snapshot({"url": "https://e.com/v.mp4"})["status"], "completed")


class KeyHandlingTest(unittest.TestCase):
    def test_16_dotenv_and_key_resolution(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, ".env")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# comment\nAGNES_TEST_KEY=abc123\nexport AGNES_OTHER='quoted'\n")
        self.assertEqual(api.load_dotenv(path), path)
        self.assertEqual(os.environ["AGNES_TEST_KEY"], "abc123")
        self.assertEqual(os.environ["AGNES_OTHER"], "quoted")
        self.assertEqual(api.resolve_api_key("explicit"), "explicit")
        self.assertEqual(api.resolve_api_key(None), "")

    def test_17_verify_key_only_rejects_real_401(self):
        original = api.fetch_models
        try:
            def unauthorized(**kwargs):
                raise api.AgnesError("invalid key", status=401)
            api.fetch_models = unauthorized
            result = api.verify_key("sk-bad")
            self.assertFalse(result["ok"])
            self.assertFalse(result["verified"])

            def firewalled(**kwargs):
                raise api.AgnesError("network error: TLS/SSL connection has been closed",
                                     status=None)
            api.fetch_models = firewalled
            result = api.verify_key("sk-good")
            self.assertTrue(result["ok"])
            self.assertFalse(result["verified"])
            self.assertIn("network", result["warning"].lower())

            def server_error(**kwargs):
                raise api.AgnesError("Internal Server Error", status=500)
            api.fetch_models = server_error
            result = api.verify_key("sk-good")
            self.assertTrue(result["ok"])
            self.assertFalse(result["verified"])
        finally:
            api.fetch_models = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
