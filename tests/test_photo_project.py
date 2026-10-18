"""照片导入：全尺寸、方向与色彩、来源、覆盖保护及已有流程兼容。"""

import base64
import contextlib
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image, ImageCms

from compositor_tool import get_project_image
from main import encode_photo, load_photo
from photo_project import create_photo_project
from run_agent import run_pipeline


class PhotoProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromatic-photo-project-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.input = self.root / "input.png"
        Image.new("RGB", (1800, 1200), (100, 130, 160)).save(self.input)
        self.output = self.root / "new.comp"

    def resource(self, project):
        _, layer, path = get_project_image(project)
        return layer, path

    def test_full_size_png_and_neutral_project_without_model(self):
        before = self.input.read_bytes()
        with patch("main.load_api_key") as key, patch("main.OpenAI") as model:
            project, report_path = create_photo_project(self.input, self.output)
        key.assert_not_called()
        model.assert_not_called()
        self.assertEqual(self.input.read_bytes(), before)
        self.assertEqual(report_path, self.root / "new.import.json")
        manifest = json.loads((project / "manifest.json").read_bytes())
        self.assertEqual((manifest["width"], manifest["height"]), (1800, 1200))
        self.assertEqual(manifest["colorSpace"], "sRGB")
        self.assertEqual(len(manifest["layers"]), 2)
        photo, balance = manifest["layers"]
        self.assertEqual(manifest["activeLayerID"], photo["id"])
        self.assertEqual(photo["transform"]["size"], [1800, 1200])
        self.assertTrue(photo["isVisible"])
        self.assertFalse(balance["isVisible"])
        self.assertEqual(balance["name"], "Test Color Balance")
        settings = balance["adjustment"]["colorBalanceSettings"]
        self.assertTrue(settings["preserveLuminosity"])
        self.assertTrue(all(value == 0 for key, value in settings.items() if key != "preserveLuminosity"))
        _, resource = self.resource(project)
        with Image.open(resource) as stored:
            self.assertEqual(stored.format, "PNG")
            self.assertEqual(stored.mode, "RGB")
            self.assertEqual(stored.size, (1800, 1200))
            self.assertEqual(stored.getpixel((1799, 1199)), (100, 130, 160))
            self.assertTrue(stored.info.get("icc_profile"))
            self.assertNotIn(274, stored.getexif())
        url, preview_source = encode_photo(resource)
        self.assertEqual(preview_source["preview_dimensions"], [1600, 1067])
        with Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1]))) as preview:
            self.assertEqual(preview.size, (1600, 1067))

    def test_source_record_binds_separate_input_and_resource_hashes(self):
        project, report_path = create_photo_project(self.input, self.output)
        layer, resource = self.resource(project)
        report = json.loads(report_path.read_bytes())
        self.assertEqual(report["source"]["image_path"], str(self.input))
        self.assertEqual(report["source"]["image_sha256"], hashlib.sha256(self.input.read_bytes()).hexdigest())
        self.assertEqual(report["source"]["original_dimensions"], [1800, 1200])
        self.assertEqual(report["project_image"]["image_sha256"], hashlib.sha256(resource.read_bytes()).hexdigest())
        self.assertEqual(report["project_image"]["image_layer_id"], layer["id"])
        self.assertEqual(report["project_image"]["image_file"], resource.name)
        self.assertEqual(report["manifest_sha256"], hashlib.sha256((project / "manifest.json").read_bytes()).hexdigest())
        self.assertEqual(report["status"], "source_project_created_pending_visual_review")
        self.assertEqual(report["project_image"]["dimensions"], [1800, 1200])

    def test_jpeg_exif_rotation_keeps_all_pixels_and_removes_orientation(self):
        photo = Image.new("RGB", (60, 40), "red")
        for x in range(30, 60):
            for y in range(40):
                photo.putpixel((x, y), (0, 0, 255))
        exif = Image.Exif()
        exif[274] = 6  # 顺时针旋转 90 度。
        source = self.root / "rotated.jpg"
        photo.save(source, quality=95, exif=exif)
        original_bytes = source.read_bytes()
        with Image.open(source) as original:
            expected_pixels = original.transpose(Image.Transpose.ROTATE_270).tobytes()
        project, report_path = create_photo_project(source, self.output)
        _, resource = self.resource(project)
        with Image.open(resource) as stored:
            self.assertEqual(stored.size, (40, 60))
            self.assertEqual(stored.tobytes(), expected_pixels)
            self.assertNotIn(274, stored.getexif())
        report = json.loads(report_path.read_bytes())
        self.assertEqual(report["source"]["original_dimensions"], [60, 40])
        self.assertEqual(report["project_image"]["dimensions"], [40, 60])
        self.assertEqual(source.read_bytes(), original_bytes)

    def test_valid_icc_and_non_rgb_inputs_produce_rgb_resources(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        for index, mode in enumerate(("RGB", "L", "P", "RGBA")):
            source = self.root / f"mode-{index}.png"
            image = Image.new(mode, (30, 20), (100, 130, 160, 255) if mode == "RGBA" else 100)
            image.save(source, icc_profile=profile if mode == "RGB" else None)
            project, _ = create_photo_project(source, self.root / f"mode-{index}.comp")
            _, resource = self.resource(project)
            with Image.open(resource) as stored:
                self.assertEqual(stored.mode, "RGB")
                self.assertEqual(stored.size, (30, 20))

    def test_invalid_images_stop_before_creating_output(self):
        invalid = []
        bad = self.root / "broken.jpg"
        bad.write_bytes(b"not an image")
        invalid.append(bad)
        gif = self.root / "other.gif"
        Image.new("RGB", (20, 30)).save(gif)
        invalid.append(gif)
        transparent = self.root / "transparent.png"
        Image.new("RGBA", (20, 30), (100, 100, 100, 0)).save(transparent)
        invalid.append(transparent)
        animated = self.root / "animated.png"
        Image.new("RGB", (20, 30), "red").save(
            animated, save_all=True, append_images=[Image.new("RGB", (20, 30), "blue")], duration=100,
        )
        invalid.append(animated)
        bad_profile = self.root / "bad-profile.png"
        Image.new("RGB", (20, 30)).save(bad_profile, icc_profile=b"invalid profile")
        invalid.append(bad_profile)
        invalid.append(self.root / "missing.jpg")
        for source in invalid:
            with self.subTest(source=source), self.assertRaises((ValueError, OSError)):
                create_photo_project(source, self.output)
            self.assertFalse(self.output.exists())
            self.assertFalse(self.output.with_suffix(".import.json").exists())

    def test_pixel_limit_checked_before_normalizing(self):
        original = MagicMock(format="PNG", n_frames=1, width=10000, height=10000)
        manager = MagicMock()
        manager.__enter__.return_value = original
        with patch("main.Image.open", return_value=manager), patch("main.ImageOps.exif_transpose") as transpose:
            with self.assertRaisesRegex(ValueError, "8000 万"):
                create_photo_project(self.input, self.output)
        transpose.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_existing_output_report_and_broken_symlinks_are_not_overwritten(self):
        for path in (self.output, self.output.with_suffix(".import.json")):
            for symlink in (False, True):
                if symlink:
                    path.symlink_to(self.root / "missing-target")
                else:
                    path.write_bytes(b"keep")
                try:
                    with self.assertRaisesRegex(ValueError, "已经存在"):
                        create_photo_project(self.input, self.output)
                    if symlink:
                        self.assertTrue(path.is_symlink())
                    else:
                        self.assertEqual(path.read_bytes(), b"keep")
                finally:
                    path.unlink()
        with self.assertRaisesRegex(ValueError, ".comp"):
            create_photo_project(self.input, self.root / "wrong-name")

    def test_independent_imports_have_unique_documents_and_layers(self):
        first, _ = create_photo_project(self.input, self.output)
        second, _ = create_photo_project(self.input, self.root / "second.comp")
        a = json.loads((first / "manifest.json").read_bytes())
        b = json.loads((second / "manifest.json").read_bytes())
        self.assertNotEqual(a["documentID"], b["documentID"])
        self.assertTrue(set(layer["id"] for layer in a["layers"]).isdisjoint(layer["id"] for layer in b["layers"]))

    def test_cannot_add_output_inside_original_project(self):
        project, _ = create_photo_project(self.input, self.output)
        _, resource = self.resource(project)
        with self.assertRaisesRegex(ValueError, "已有工程内部"):
            create_photo_project(resource, project / "nested.comp")
        self.assertFalse((project / "nested.comp").exists())

    def test_partial_files_preserved_after_write_failure(self):
        with patch("photo_project.get_project_image", side_effect=ValueError("模拟资源检查失败")):
            with self.assertRaisesRegex(ValueError, "部分工程保留"):
                create_photo_project(self.input, self.output)
        self.assertTrue((self.output / "manifest.json").exists())
        self.assertTrue(list((self.output / "images").glob("*.png")))
        self.assertFalse(self.output.with_suffix(".import.json").exists())

    def test_input_changes_during_import_do_not_produce_success_record(self):
        real_get = get_project_image

        def change(project):
            self.input.write_bytes(self.input.read_bytes() + b"changed")
            return real_get(project)

        with patch("photo_project.get_project_image", side_effect=change):
            with self.assertRaisesRegex(ValueError, "导入期间发生变化"):
                create_photo_project(self.input, self.output)
        self.assertTrue(self.output.exists())
        self.assertFalse(self.output.with_suffix(".import.json").exists())

    def test_original_file_format_detected_by_content(self):
        disguised = self.root / "photo.data"
        disguised.write_bytes(self.input.read_bytes())
        project, _ = create_photo_project(disguised, self.output)
        _, resource = self.resource(project)
        with Image.open(resource) as stored:
            self.assertEqual(stored.size, (1800, 1200))

    def test_existing_analysis_and_adjustment_flow_accepts_imported_project(self):
        project, _ = create_photo_project(self.input, self.output)
        result = {
            "analysis": {k: "保持原样" for k in ("exposure", "white_balance", "contrast", "color_tendency")},
            "adjustment_plan": {"warmth_adjustment": 0, "exposure_ev": 0,
                                "contrast_adjustment": 0, "saturation_adjustment": 0},
        }
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value=json.dumps(result),
        ) as request, contextlib.redirect_stdout(StringIO()):
            paths = run_pipeline("保持原样", project, self.root / "mock-run")
        self.assertEqual(request.call_count, 1)
        _, original = self.resource(project)
        _, copied = self.resource(paths[1])
        self.assertEqual(original.read_bytes(), copied.read_bytes())
        report = json.loads(paths[2].read_bytes())
        self.assertEqual(report["source"]["project_path"], str(project))
        self.assertEqual(report["not_applied_adjustments"], {})

    def test_full_size_reader_does_not_resize_and_preview_is_independent(self):
        image, source = load_photo(self.input)
        with image:
            self.assertEqual(image.size, (1800, 1200))
            self.assertEqual(source["original_dimensions"], [1800, 1200])
            _, preview = encode_photo(self.input)
            self.assertEqual(preview["preview_dimensions"], [1600, 1067])
            self.assertEqual(image.size, (1800, 1200))


if __name__ == "__main__":
    unittest.main()
