"""基础对比度测试：合法曲线、方向、隔离、复用和入口兼容性。"""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from compositor_tool import (
    CONTRAST_LAYER_NAME, EXPOSURE_LAYER_NAME, SATURATION_LAYER_NAME, TARGET_LAYER_NAME,
    create_adjusted_project, map_contrast_to_curves,
)


class ContrastTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromatic-contrast-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source.comp"
        (self.source / "images").mkdir(parents=True)
        (self.source / "images/photo.png").write_bytes(b"original-image-resource")
        self.manifest = {
            "format": "com.compositor.project", "width": 80, "height": 120,
            "layers": [
                {"id": "photo-id", "name": "Photo", "imageFile": "photo.png", "isVisible": True},
                {"id": "old-curves", "name": "Test Curves", "isVisible": False,
                 "adjustment": {"kind": "Curves", "curves": {"unchanged": True}}},
                {"id": "balance-id", "name": TARGET_LAYER_NAME, "isVisible": False,
                 "adjustment": {"kind": "Color Balance"}},
            ],
        }
        (self.source / "manifest.json").write_text(json.dumps(self.manifest))

    def read_layers(self, project):
        return json.loads((project / "manifest.json").read_text())["layers"]

    def test_curve_constraints_direction_and_fractional_suggestion(self):
        for value in [-1, -0.5, 0, 0.15, 0.5, 1]:
            curves = map_contrast_to_curves(value)
            self.assertEqual(curves["channel"], "RGB")
            self.assertEqual(len(curves["channels"]), 4)
            master = curves["channels"][0]
            self.assertEqual(master[0], {"x": 0, "y": 0})
            self.assertEqual(master[-1], {"x": 255, "y": 255})
            self.assertEqual(master[2], {"x": 128, "y": 128})
            self.assertTrue(all(0 <= p["x"] <= 255 and 0 <= p["y"] <= 255 for p in master))
            self.assertTrue(all(a["x"] < b["x"] and a["y"] < b["y"]
                                for a, b in zip(master, master[1:])))
            if value > 0:
                self.assertLess(master[1]["y"], 64)
                self.assertGreater(master[3]["y"], 192)
            elif value < 0:
                self.assertGreater(master[1]["y"], 64)
                self.assertLess(master[3]["y"], 192)
            else:
                self.assertTrue(all(p["x"] == p["y"] for p in master))
            for channel in curves["channels"][1:]:
                self.assertEqual(channel, [{"x": 0, "y": 0}, {"x": 255, "y": 255}])
        master = map_contrast_to_curves(0.15)["channels"][0]
        self.assertAlmostEqual(master[1]["y"], 59.2)
        self.assertAlmostEqual(master[3]["y"], 196.8)

    def test_invalid_values_stop_before_copy(self):
        for value in [True, False, "0.5", float("nan"), float("inf"),
                      float("-inf"), -1.01, 1.01, 10**500]:
            with self.subTest(value=str(value)):
                with self.assertRaises(ValueError):
                    map_contrast_to_curves(value)
                output = self.root / "invalid.comp"
                with self.assertRaises(ValueError):
                    create_adjusted_project(self.source, output, 0, contrast_adjustment=value)
                self.assertFalse(output.exists())
        with self.assertRaises(ValueError):
            map_contrast_to_curves(None)

    def test_fixed_projects_preserve_source_resources_and_old_curves(self):
        original = (self.source / "manifest.json").read_bytes()
        for index, value in enumerate([-0.5, 0, 0.5]):
            output = create_adjusted_project(
                self.source, self.root / f"fixed-{index}.comp", 0, contrast_adjustment=value,
            )
            layers = self.read_layers(output)
            self.assertEqual(len(layers), 4)
            self.assertEqual(layers[:2], self.manifest["layers"][:2])
            layer = layers[-1]
            self.assertEqual(layer["name"], CONTRAST_LAYER_NAME)
            self.assertEqual(layer["adjustment"]["kind"], "Curves")
            self.assertEqual(layer["adjustment"]["curves"], map_contrast_to_curves(value))
            self.assertTrue(layer["isVisible"])
            self.assertEqual(layer["opacity"], 1)
            self.assertEqual(layer["blendMode"], "Normal")
            self.assertNotIn("imageFile", layer)
            self.assertEqual(str(uuid.UUID(layer["id"])).upper(), layer["id"])
            self.assertEqual(layer["transform"]["size"], [80, 120])
            self.assertEqual(layer["adjustment"]["levels"]["ranges"][0]["gamma"], 1)
            self.assertEqual((output / "images/photo.png").read_bytes(), b"original-image-resource")
        self.assertEqual((self.source / "manifest.json").read_bytes(), original)

    def test_all_four_tools_reuse_layers_and_replace_curves(self):
        first = create_adjusted_project(self.source, self.root / "first.comp", 0.25, 0.1, -0.1, 0.5)
        layers = self.read_layers(first)
        expected = [TARGET_LAYER_NAME, EXPOSURE_LAYER_NAME, CONTRAST_LAYER_NAME, SATURATION_LAYER_NAME]
        self.assertEqual([p["name"] for p in layers[-4:]], expected)
        tool_ids = [p["id"] for p in layers[-4:]]
        manifest_path = first / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        # 单独颜色曲线若曾被修改，重新执行也应恢复约定的中性通道。
        manifest["layers"][-2]["adjustment"]["curves"]["channels"][1][0]["y"] = 20
        manifest_path.write_text(json.dumps(manifest))
        second = create_adjusted_project(first, self.root / "second.comp", 0.1, -0.1, 0.2, -0.5)
        layers = self.read_layers(second)
        self.assertEqual([p["name"] for p in layers[-4:]], expected)
        self.assertEqual([p["id"] for p in layers[-4:]], tool_ids)
        self.assertEqual(len(layers), 6)
        self.assertEqual(layers[-2]["adjustment"]["curves"], map_contrast_to_curves(-0.5))
        self.assertEqual(layers[1], self.manifest["layers"][1])

    def test_omitted_contrast_keeps_old_call_behavior_and_existing_curve(self):
        old = create_adjusted_project(self.source, self.root / "old.comp", 0.2, 0.1, -0.1)
        self.assertNotIn(CONTRAST_LAYER_NAME, [p["name"] for p in self.read_layers(old)])
        first = create_adjusted_project(old, self.root / "first.comp", 0, contrast_adjustment=0.3)
        curve = copy.deepcopy(self.read_layers(first)[-2])
        second = create_adjusted_project(first, self.root / "second.comp", 0, 0.1, 0.2)
        layers = self.read_layers(second)
        self.assertEqual(layers[-2], curve)
        self.assertEqual([p["name"] for p in layers[-4:]],
                         [TARGET_LAYER_NAME, EXPOSURE_LAYER_NAME, CONTRAST_LAYER_NAME, SATURATION_LAYER_NAME])

    def test_name_conflicts_stop_before_copy(self):
        for extras in [
            [{"id": "wrong", "name": CONTRAST_LAYER_NAME, "adjustment": {"kind": "Exposure"}}],
            [{"id": "a", "name": CONTRAST_LAYER_NAME, "adjustment": {"kind": "Curves"}},
             {"id": "b", "name": CONTRAST_LAYER_NAME, "adjustment": {"kind": "Curves"}}],
            [{"id": "group", "name": CONTRAST_LAYER_NAME, "isGroup": True,
              "adjustment": {"kind": "Curves"}}],
            [{"id": "photo-contrast", "name": CONTRAST_LAYER_NAME, "imageFile": "photo.png",
              "adjustment": {"kind": "Curves"}}],
        ]:
            manifest = copy.deepcopy(self.manifest)
            manifest["layers"].extend(extras)
            (self.source / "manifest.json").write_text(json.dumps(manifest))
            output = self.root / "conflict.comp"
            with self.assertRaises(ValueError):
                create_adjusted_project(self.source, output, 0, contrast_adjustment=0.5)
            self.assertFalse(output.exists())

    def test_cli_accepts_contrast_and_rejects_out_of_range(self):
        output = self.root / "cli.comp"
        args = [sys.executable, "compositor_tool.py", "--input", str(self.source),
                "--output", str(output), "--warmth", "0", "--contrast"]
        result = subprocess.run([*args, "-0.5"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("对比度建议值：-0.5", result.stdout)
        self.assertEqual(self.read_layers(output)[-1]["adjustment"]["curves"], map_contrast_to_curves(-0.5))
        invalid = self.root / "invalid-cli.comp"
        args[args.index(str(output))] = str(invalid)
        result = subprocess.run([*args, "2"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("对比度参数", result.stderr)
        self.assertFalse(invalid.exists())


if __name__ == "__main__":
    unittest.main()
