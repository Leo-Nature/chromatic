"""针对照片错配问题的离线回归测试，不调用模型。"""

import base64
import contextlib
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from apply_analysis import apply_saved_analysis
from compositor_tool import create_adjusted_project, get_project_image
from main import encode_photo
from run_agent import PipelineError, run_pipeline


class ProjectSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromatic-source-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.make_project("source.comp", "red")
        self.result = {
            "analysis": {key: "建议保持自然" for key in (
                "exposure", "white_balance", "contrast", "color_tendency"
            )},
            "adjustment_plan": {
                "exposure_ev": -0.2, "contrast_adjustment": -0.1,
                "warmth_adjustment": 0.15, "saturation_adjustment": -0.1,
            },
        }

    def make_project(self, name, color):
        source = self.root / name
        (source / "images").mkdir(parents=True)
        Image.new("RGB", (80, 120), color).save(source / "images/photo.png")
        manifest = {
            "format": "com.compositor.project",
            "width": 80, "height": 120,
            "layers": [
                {"id": "photo-id", "name": "Photo", "imageFile": "photo.png", "isVisible": True},
                {"id": "balance-id", "name": "Test Color Balance", "isVisible": False,
                 "adjustment": {"kind": "Color Balance"}},
            ],
        }
        (source / "manifest.json").write_text(json.dumps(manifest))
        return source

    def run_offline(self, name="run", side_effect=None):
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value=json.dumps(self.result),
            side_effect=side_effect,
        ) as request, contextlib.redirect_stdout(StringIO()):
            paths = run_pipeline("稍微暖一点", self.source, self.root / name)
        return paths, request

    def test_uploaded_preview_is_saved_and_bound_to_output(self):
        original = (self.source / "images/photo.png").read_bytes()
        paths, request = self.run_offline()
        analysis_path, project, report_path, _ = paths
        uploaded = base64.b64decode(request.call_args.args[0].split(",", 1)[1])
        preview = analysis_path.parent / "analysis-preview.jpg"
        self.assertEqual(uploaded, preview.read_bytes())
        metadata = json.loads(analysis_path.read_text())["metadata"]
        self.assertEqual(metadata["image_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(metadata["preview_sha256"], hashlib.sha256(uploaded).hexdigest())
        self.assertEqual(metadata["project_source"]["image_layer_id"], "photo-id")
        self.assertEqual((project / "images/photo.png").read_bytes(), original)
        report = json.loads(report_path.read_text())
        self.assertTrue(report["project_photo_verified"])
        self.assertEqual(report["applied_adjustments"], {
            "warmth_adjustment": 0.15, "saturation_adjustment": -0.1, "exposure_ev": -0.2,
            "contrast_adjustment": -0.1,
        })
        self.assertEqual(report["not_applied_adjustments"], {})
        self.assertEqual(report["exposure_settings"], {"exposure": -0.2, "offset": 0, "gamma": 1})
        layers = json.loads((project / "manifest.json").read_text())["layers"]
        self.assertEqual([layer["name"] for layer in layers[-4:]], report["tool_layer_order_bottom_to_top"])

    def test_wrong_project_rejected_even_with_same_layer_id(self):
        paths, _ = self.run_offline()
        other = self.make_project("other.comp", "blue")
        out = self.root / "wrong.comp"
        with self.assertRaisesRegex(ValueError, "原图指纹"):
            apply_saved_analysis(paths[0], other, out)
        self.assertFalse(out.exists())

    def test_legacy_analysis_without_project_binding_rejected(self):
        photo = self.source / "images/photo.png"
        legacy = {**self.result, "metadata": {
            "image_sha256": hashlib.sha256(photo.read_bytes()).hexdigest()
        }}
        analysis = self.root / "legacy.json"
        analysis.write_text(json.dumps(legacy))
        with self.assertRaisesRegex(ValueError, "未绑定"):
            apply_saved_analysis(analysis, self.source, self.root / "legacy.comp")

    def test_layer_reference_change_rejected(self):
        paths, _ = self.run_offline()
        manifest_path = self.source / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["layers"][0]["id"] = "changed-id"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "图层与目标工程不一致"):
            apply_saved_analysis(paths[0], self.source, self.root / "changed.comp")

    def test_multiple_images_rejected_before_request(self):
        manifest_path = self.source / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["layers"].append({**manifest["layers"][0], "id": "second-id"})
        manifest_path.write_text(json.dumps(manifest))
        with patch("run_agent.request_analysis") as request, contextlib.redirect_stdout(StringIO()):
            with self.assertRaisesRegex(PipelineError, "恰好包含一个图片"):
                run_pipeline("暖一点", self.source, self.root / "invalid")
        request.assert_not_called()
        self.assertFalse((self.root / "invalid").exists())

    def test_groups_and_hidden_images_rejected(self):
        path = self.source / "manifest.json"
        original = json.loads(path.read_text())
        for key, value in [("isGroup", True), ("isVisible", False)]:
            manifest = json.loads(json.dumps(original))
            manifest["layers"][0][key] = value
            path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                get_project_image(self.source)

    def test_photo_change_during_request_preserves_analysis_but_stops_execution(self):
        def change_photo(*args):
            Image.new("RGB", (80, 120), "green").save(self.source / "images/photo.png")
            return json.dumps(self.result)
        with self.assertRaisesRegex(PipelineError, "原图指纹"):
            self.run_offline(side_effect=change_photo)
        self.assertTrue((self.root / "run/analysis.json").exists())
        self.assertFalse((self.root / "run/result.comp").exists())

    def test_copy_changed_after_check_never_reports_verified_success(self):
        paths, _ = self.run_offline()
        def changed_copy(source, output, warmth, saturation_adjustment=None, exposure_ev=None, contrast_adjustment=None):
            create_adjusted_project(source, output, warmth, saturation_adjustment, exposure_ev, contrast_adjustment)
            Image.new("RGB", (80, 120), "blue").save(output / "images/photo.png")
        output = self.root / "raced.comp"
        with patch("apply_analysis.create_adjusted_project", side_effect=changed_copy):
            with self.assertRaisesRegex(ValueError, "输出工程图片与分析来源不一致"):
                apply_saved_analysis(paths[0], self.source, output)
        self.assertFalse(output.with_suffix(".report.json").exists())

    def test_preview_resizes_preserves_ratio_and_exif_orientation(self):
        image = Image.new("RGB", (2400, 1200), "red")
        exif = Image.Exif()
        exif[274] = 6
        path = self.root / "rotated.jpg"
        image.save(path, exif=exif)
        before = path.read_bytes()
        url, metadata = encode_photo(path)
        with Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1]))) as preview:
            self.assertEqual(preview.size, (800, 1600))
        self.assertEqual(metadata["preview_dimensions"], [800, 1600])
        self.assertEqual(path.read_bytes(), before)

    def test_bad_image_and_output_conflicts(self):
        path = self.root / "bad.png"
        path.write_bytes(b"not an image")
        with self.assertRaises(OSError):
            encode_photo(path)
        paths, _ = self.run_offline()
        with self.assertRaisesRegex(ValueError, "已经存在"):
            apply_saved_analysis(paths[0], self.source, paths[1])
        output = self.root / "report-conflict.comp"
        output.with_suffix(".report.json").write_text("keep")
        with self.assertRaisesRegex(ValueError, "执行记录已经存在"):
            apply_saved_analysis(paths[0], self.source, output)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
