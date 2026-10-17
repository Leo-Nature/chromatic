"""参考作品模式的离线测试：输入身份、字段保存和来源追溯。"""

import base64
import contextlib
import copy
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from apply_analysis import apply_saved_analysis
from main import REFERENCE_FIELDS, request_analysis, validate_analysis
from run_agent import PipelineError, run_pipeline


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromatic-reference-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source.comp"
        (self.source / "images").mkdir(parents=True)
        Image.new("RGB", (80, 120), "red").save(self.source / "images/photo.png")
        manifest = {
            "width": 80, "height": 120,
            "format": "com.compositor.project", "layers": [
                {"id": "photo-id", "name": "Photo", "imageFile": "photo.png", "isVisible": True},
                {"id": "balance-id", "name": "Test Color Balance", "isVisible": False,
                 "adjustment": {"kind": "Color Balance"}},
            ],
        }
        (self.source / "manifest.json").write_text(json.dumps(manifest))
        self.reference = self.root / "style.jpg"
        Image.new("RGB", (90, 160), "orange").save(self.reference)
        self.basic = {
            "analysis": {key: "原图分析" for key in (
                "exposure", "white_balance", "contrast", "color_tendency"
            )},
            "adjustment_plan": {
                "exposure_ev": -0.1, "contrast_adjustment": 0,
                "warmth_adjustment": 0.2, "saturation_adjustment": -0.1,
            },
        }
        self.result = {
            **self.basic,
            "reference_analysis": {key: "参考作品特征" for key in REFERENCE_FIELDS},
            "transfer_explanation": "借鉴暖调，保留原图主体和高光。",
        }

    def run_reference(self, result=None, reference=None, output="run"):
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value=json.dumps(result or self.result)
        ) as request, contextlib.redirect_stdout(StringIO()):
            paths = run_pipeline(
                "借鉴暖色，保留细节", self.source, self.root / output,
                reference_path=reference or self.reference,
            )
        return paths, request

    def test_request_labels_images_and_keeps_original_first(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.chat.completions.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="{}", refusal=None))
        ])
        with patch("main.OpenAI", return_value=client):
            request_analysis("original-url", "借鉴风格", "mock", reference_url="reference-url")
            args = client.chat.completions.create.call_args.kwargs
            parts = args["messages"][1]["content"]
            urls = [part["image_url"]["url"] for part in parts if part["type"] == "image_url"]
            self.assertEqual(urls, ["original-url", "reference-url"])
            self.assertIn("图片 A", parts[1]["text"])
            self.assertIn("图片 B", parts[3]["text"])
            self.assertIn("reference_analysis", args["messages"][0]["content"])
            request_analysis("original-url", "自然", "mock")
            parts = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
            self.assertEqual(sum(p["type"] == "image_url" for p in parts), 1)

    def test_reference_fields_survive_save_execution_and_report(self):
        paths, request = self.run_reference()
        analysis, project, report_path, result = paths
        document = json.loads(analysis.read_text())
        report = json.loads(report_path.read_text())
        self.assertEqual(request.call_count, 1)
        self.assertEqual(document["reference_analysis"], self.result["reference_analysis"])
        self.assertEqual(report["transfer_explanation"], self.result["transfer_explanation"])
        self.assertEqual(result["reference_analysis"], self.result["reference_analysis"])
        reference_bytes = base64.b64decode(request.call_args.kwargs["reference_url"].split(",", 1)[1])
        self.assertEqual(reference_bytes, (analysis.parent / "reference-preview.jpg").read_bytes())
        reference_source = document["metadata"]["reference"]
        self.assertEqual(reference_source["image_sha256"], hashlib.sha256(self.reference.read_bytes()).hexdigest())
        self.assertEqual(reference_source["preview_sha256"], hashlib.sha256(reference_bytes).hexdigest())
        self.assertEqual(report["source"]["reference"]["image_sha256"], reference_source["image_sha256"])
        self.assertEqual((project / "images/photo.png").read_bytes(), (self.source / "images/photo.png").read_bytes())
        self.assertNotEqual((project / "images/photo.png").read_bytes(), self.reference.read_bytes())
        self.assertEqual(report["applied_adjustments"], {
            "warmth_adjustment": 0.2, "saturation_adjustment": -0.1, "exposure_ev": -0.1,
            "contrast_adjustment": 0,
        })
        self.assertEqual(report["not_applied_adjustments"], {})
        self.assertEqual(report["exposure_settings"], {"exposure": -0.1, "offset": 0, "gamma": 1})
        self.assertIn("不代表完整风格迁移", report["execution_scope"])
        self.assertEqual(report["contrast_curves"]["channels"][0],
                         [{"x": n, "y": n} for n in [0, 64, 128, 192, 255]])

    def test_missing_or_corrupt_reference_stops_before_request(self):
        bad = self.root / "bad.jpg"
        bad.write_bytes(b"not an image")
        for reference in (bad, self.root / "missing.jpg"):
            with patch("run_agent.request_analysis") as request, contextlib.redirect_stdout(StringIO()):
                with self.assertRaises(PipelineError):
                    run_pipeline("参考风格", self.source, self.root / "failed", reference_path=reference)
            request.assert_not_called()
            self.assertFalse((self.root / "failed").exists())

    def test_missing_reference_analysis_never_executes(self):
        with patch("run_agent.apply_saved_analysis") as execute:
            with self.assertRaisesRegex(PipelineError, "reference_analysis"):
                self.run_reference(result=self.basic)
        execute.assert_not_called()
        self.assertFalse((self.root / "run/analysis.json").exists())

    def test_reference_fields_are_required_and_not_dropped_on_revalidation(self):
        for key in REFERENCE_FIELDS:
            result = copy.deepcopy(self.result)
            result["reference_analysis"][key] = " "
            with self.assertRaises(ValueError):
                validate_analysis(result, require_reference=True)
        result = copy.deepcopy(self.result)
        result["transfer_explanation"] = " "
        with self.assertRaises(ValueError):
            validate_analysis(result, require_reference=True)
        once = validate_analysis(self.result, require_reference=True)
        self.assertEqual(validate_analysis(once), once)
        self.assertEqual(validate_analysis(self.basic), self.basic)

    def test_saved_analysis_retry_uses_snapshot_not_current_reference_file(self):
        paths, _ = self.run_reference()
        self.reference.unlink()
        _, report = apply_saved_analysis(paths[0], self.source, self.root / "retry.comp")
        self.assertEqual(json.loads(report.read_text())["analysis_mode"], "reference")

    def test_reference_content_without_source_is_rejected(self):
        paths, _ = self.run_reference()
        document = json.loads(paths[0].read_text())
        for bad_reference in (None, {}, {"image_path": "style.jpg"}):
            document["metadata"]["reference"] = bad_reference
            bad = self.root / "bad-analysis.json"
            bad.write_text(json.dumps(document))
            with self.assertRaises(ValueError):
                apply_saved_analysis(bad, self.source, self.root / "bad.comp")
            self.assertFalse((self.root / "bad.comp").exists())


if __name__ == "__main__":
    unittest.main()
