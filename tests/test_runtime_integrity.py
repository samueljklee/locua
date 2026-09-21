import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from locua.engine import runtime_paths


class RuntimeIntegrityTests(unittest.TestCase):
    def check_rejected(self, record, message):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "probes").mkdir()
            (root / "probes/baseline-files.json").write_text(json.dumps(record))
            (root / "probes/model_compare_v3_models.json").write_text(json.dumps({"models": {"baseline": {"model_id": "expected", "revision": "fixed"}}}))
            with patch.object(runtime_paths, "ROOT", root), patch.object(runtime_paths, "model_cache") as cache:
                with self.assertRaisesRegex(RuntimeError, message):
                    runtime_paths.verify_model("baseline")
                cache.assert_not_called()

    def test_stale_model_manifest_is_not_reported_as_original_baseline(self):
        self.check_rejected({"model": {"model_id": "different", "revision": "fixed"}, "files": []}, "configured model pin")

    def test_empty_or_path_escaping_model_manifest_fails_before_loading(self):
        record = {"model": {"model_id": "expected", "revision": "fixed"}, "files": []}
        self.check_rejected(record, "incomplete")
        record["files"] = [{"path": n} for n in ("../model.safetensors", "config.json", "tokenizer.json", "tokenizer_config.json")]
        self.check_rejected(record, "unsafe")


if __name__ == "__main__":
    unittest.main()
