"""成片只读展示：严格版本绑定、原字节下载、损坏与页面切换。"""

import contextlib
import copy
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin
from streamlit.testing.v1 import AppTest

from compositor_tool import get_project_image
from export_view import load_export_for_report, make_png_preview
from refine_agent import run_refinement
from run_agent import run_pipeline


def show_export_ui(output_dir):
    from app import render_page
    render_page(output_dir)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ExportViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromatic-export-view-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.outputs = self.root / "outputs"
        self.source = self.outputs / "sources/original.comp"
        (self.source / "images").mkdir(parents=True)
        Image.new("RGB", (80, 120), "red").save(self.source / "images/photo.png")
        (self.source / "manifest.json").write_text(json.dumps({
            "format": "com.compositor.project", "width": 80, "height": 120,
            "layers": [
                {"id": "photo-id", "imageFile": "photo.png", "name": "Photo", "isVisible": True},
                {"id": "balance-id", "name": "Test Color Balance", "isVisible": False,
                 "adjustment": {"kind": "Color Balance"}},
            ],
        }))
        self.result = {
            "analysis": {key: "保持自然" for key in ("exposure", "white_balance", "contrast", "color_tendency")},
            "adjustment_plan": {"warmth_adjustment": 0.15, "exposure_ev": 0,
                                "contrast_adjustment": 0.1, "saturation_adjustment": 0.1},
        }
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value=json.dumps(self.result)
        ), contextlib.redirect_stdout(StringIO()):
            self.initial = run_pipeline("保持自然", self.source, self.outputs / "runs/initial")
        response = copy.deepcopy(self.result)
        response["feedback_explanation"] = "保留其他参数，减少暖感。"
        response["adjustment_plan"]["warmth_adjustment"] = 0.05
        with patch("refine_agent.load_api_key", return_value="mock"), patch(
            "refine_agent.request_analysis", return_value=json.dumps(response)
        ), contextlib.redirect_stdout(StringIO()):
            self.feedback = run_refinement(self.initial[2], self.source, "减少暖感", self.outputs / "runs/feedback")
        # 合成像素仅用作测试夹具，不是 Python 渲染 Compositor 的实现。
        self.png = self.initial[0].parent / "result.png"
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("fixture", "保留这段元数据以检测下载是否被重新编码")
        metadata.add(b"sRGB", b"\0")
        Image.new("RGBA", (80, 120), (210, 50, 20, 255)).save(self.png, pnginfo=metadata)
        self.data = self.png.read_bytes()
        _, layer, resource = get_project_image(self.initial[1])
        self.document = {
            "status": "native_export_user_reviewed",
            "verification": {
                "report_path": str(self.initial[2]), "report_sha256": digest(self.initial[2]),
                "source_project": str(self.initial[1]),
                "source_manifest_sha256": digest(self.initial[1] / "manifest.json"),
                "original_project": str(self.source), "image_layer_id": layer["id"],
                "image_resource": str(resource), "image_resource_sha256": digest(resource),
                "adjustment_plan": self.result["adjustment_plan"],
                "output": {"path": str(self.png), "sha256": digest(self.png), "bytes": len(self.data),
                           "format": "PNG", "dimensions": [80, 120], "mode": "RGBA"},
            },
        }
        self.record = self.outputs / "checks/native-export-check/export-record.json"
        self.record.parent.mkdir(parents=True)
        self.save_record()

    def save_record(self):
        self.record.write_text(json.dumps(self.document), encoding="utf-8")

    def load(self):
        return load_export_for_report(self.outputs, self.initial[2])

    def page(self):
        return AppTest.from_function(show_export_ui, args=[str(self.outputs)], default_timeout=45)

    def test_exact_download_bytes_and_source_verified_without_api_or_writes(self):
        before = {str(p): digest(p) for p in self.outputs.rglob("*") if p.is_file()}
        with patch("main.OpenAI", side_effect=AssertionError("不得调用模型")):
            exported = self.load()
        self.assertEqual(exported["png_bytes"], self.data)
        self.assertEqual(exported["dimensions"], [80, 120])
        self.assertTrue(exported["user_reviewed"])
        with Image.open(BytesIO(exported["preview_bytes"])) as image:
            self.assertEqual(image.size, (80, 120))
            self.assertEqual(image.info["srgb"], 0)
        self.assertEqual(before, {str(p): digest(p) for p in self.outputs.rglob("*") if p.is_file()})

    def test_other_version_never_uses_same_named_unregistered_png(self):
        (self.feedback[0].parent / "result.png").write_bytes(self.data)
        self.assertIsNone(load_export_for_report(self.outputs, self.feedback[2]))

    def test_old_record_location_remains_supported(self):
        legacy = self.outputs / "native-export-check/export-record.json"
        legacy.parent.mkdir()
        self.record.rename(legacy)
        self.assertEqual(self.load()["png_bytes"], self.data)

    def test_report_or_manifest_changes_are_rejected(self):
        for path, label in ((self.initial[2], "执行报告"), (self.initial[1] / "manifest.json", "工程配置")):
            with self.subTest(label=label):
                before = path.read_bytes()
                path.write_bytes(before + b"\n")
                with self.assertRaisesRegex(ValueError, label):
                    self.load()
                path.write_bytes(before)

    def test_changed_png_is_not_hidden_by_cached_result(self):
        self.load()
        self.png.write_bytes(self.data + b"changed")
        with self.assertRaisesRegex(ValueError, "成片.*指纹"):
            self.load()

    def test_source_project_resource_and_plan_mismatch_are_rejected(self):
        original = copy.deepcopy(self.document)
        for key, wrong in (("source_project", str(self.feedback[1])),
                           ("image_layer_id", "wrong-id"),
                           ("image_resource", str(self.source / "images/photo.png")),
                           ("image_resource_sha256", "0" * 64),
                           ("adjustment_plan", {**self.result["adjustment_plan"], "warmth_adjustment": 0.5})):
            with self.subTest(field=key):
                self.document = copy.deepcopy(original)
                self.document["verification"][key] = wrong
                self.save_record()
                with self.assertRaises(ValueError):
                    self.load()

    def test_missing_corrupt_wrong_format_or_wrong_dimensions_are_rejected(self):
        original = copy.deepcopy(self.document)
        wrong_size = BytesIO()
        Image.new("RGB", (40, 60), "blue").save(wrong_size, format="PNG")
        jpeg = BytesIO()
        Image.new("RGB", (80, 120), "blue").save(jpeg, format="JPEG")
        for data in (None, b"broken PNG", wrong_size.getvalue(), jpeg.getvalue()):
            with self.subTest(data_type="missing" if data is None else data[:8]):
                self.document = copy.deepcopy(original)
                if data is None:
                    self.png.unlink()
                else:
                    self.png.write_bytes(data)
                    self.document["verification"]["output"].update(sha256=digest(self.png), bytes=len(data))
                self.save_record()
                with self.assertRaises(ValueError):
                    self.load()
        self.png.write_bytes(self.data)

    def test_bad_record_and_duplicate_bindings_fail_closed(self):
        self.record.write_text("not JSON")
        with self.assertRaises(ValueError):
            self.load()
        self.save_record()
        duplicate = self.outputs / "checks/exports/duplicate/export-record.json"
        duplicate.parent.mkdir(parents=True)
        duplicate.write_bytes(self.record.read_bytes())
        with self.assertRaisesRegex(ValueError, "多份导出记录"):
            self.load()

    def test_invalid_status_and_output_metadata_are_rejected(self):
        original = copy.deepcopy(self.document)
        changes = [("dimensions", [81, 120]), ("bytes", 1), ("format", "JPEG"), ("mode", "RGB")]
        for key, wrong in changes:
            with self.subTest(field=key):
                self.document = copy.deepcopy(original)
                self.document["verification"]["output"][key] = wrong
                self.save_record()
                with self.assertRaises(ValueError):
                    self.load()
        self.document = copy.deepcopy(original)
        self.document["status"] = "unverified"
        self.save_record()
        with self.assertRaisesRegex(ValueError, "尚未达到"):
            self.load()

    def test_png_outside_output_root_is_not_served(self):
        outside = self.root / "outside.png"
        outside.write_bytes(self.data)
        self.document["verification"]["output"]["path"] = str(outside)
        self.save_record()
        with self.assertRaisesRegex(ValueError, "输出目录内"):
            self.load()

    def test_preview_resizes_preserves_alpha_and_does_not_reencode_download_source(self):
        data = BytesIO()
        Image.new("RGBA", (1800, 2400), (10, 20, 30, 200)).save(data, format="PNG")
        original = data.getvalue()
        preview, dimensions, mode = make_png_preview(original, [1800, 2400])
        self.assertEqual(dimensions, [1200, 1600])
        self.assertEqual(mode, "RGBA")
        self.assertEqual(data.getvalue(), original)
        with Image.open(BytesIO(preview)) as image:
            self.assertEqual(image.getchannel("A").getextrema(), (200, 200))

    def test_preview_pixel_limit_and_direction_transform_are_rejected(self):
        with patch("export_view.MAX_EXPORT_PIXELS", 100), self.assertRaisesRegex(ValueError, "像素"):
            make_png_preview(self.data, [80, 120])
        data = BytesIO()
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (80, 120), "blue").save(data, format="PNG", exif=exif)
        with self.assertRaisesRegex(ValueError, "方向变换"):
            make_png_preview(data.getvalue(), [80, 120])

    def test_page_switch_refresh_and_original_bytes_passed_to_download(self):
        import streamlit as st
        with patch("main.OpenAI", side_effect=AssertionError("页面不得调用模型")), patch(
            "app.st.download_button", wraps=st.download_button
        ) as download:
            page = self.page().run()
            self.assertFalse(page.exception)
            self.assertEqual(page.selectbox[0].value, str(self.feedback[2]))
            download.assert_not_called()
            page.selectbox[0].select(str(self.initial[2])).run()
            self.assertFalse(page.exception)
            self.assertEqual(len(page.image), 2)
            self.assertEqual(download.call_args.kwargs["data"], self.data)
            self.assertEqual(download.call_args.kwargs["mime"], "image/png")
            self.assertEqual(download.call_args.kwargs["on_click"], "ignore")
            page.button(key="refresh_records").click().run()
            self.assertEqual(download.call_args.kwargs["data"], self.data)
            download.reset_mock()
            page.selectbox[0].select(str(self.feedback[2])).run()
            self.assertFalse(page.exception)
            self.assertEqual(len(page.image), 1)
            self.assertEqual(len(page.get("download_button")), 0)
            download.assert_not_called()

    def test_page_invalid_export_keeps_analysis_but_omits_download(self):
        self.png.write_bytes(b"changed")
        page = self.page().run()
        page.selectbox[0].select(str(self.initial[2])).run()
        self.assertFalse(page.exception)
        self.assertTrue(page.warning)
        self.assertEqual(len(page.metric), 4)
        self.assertEqual(len(page.image), 1)
        self.assertEqual(len(page.get("download_button")), 0)

    def test_changes_during_preview_stop_display_and_download(self):
        from export_view import make_png_preview as original
        def change_record(data, dimensions):
            value = original(data, dimensions)
            self.record.write_bytes(self.record.read_bytes() + b"\n")
            return value
        with patch("export_view.make_png_preview", side_effect=change_record):
            with self.assertRaisesRegex(ValueError, "准备预览期间变化"):
                self.load()


if __name__ == "__main__":
    unittest.main()
