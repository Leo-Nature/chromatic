"""曝光工具测试：EV 单位、隔离、执行顺序、兼容性及拒绝错误输入。"""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from compositor_tool import (
    EXPOSURE_LAYER_NAME, SATURATION_LAYER_NAME, TARGET_LAYER_NAME,
    create_adjusted_project, map_exposure_ev,
)


class ExposureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromatic-exposure-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source.comp"
        (self.source / "images").mkdir(parents=True)
        (self.source / "images/photo.png").write_bytes(b"original-image-resource")
        self.manifest = {
            "format": "com.compositor.project", "width": 80, "height": 120,
            "layers": [
                {"id": "photo-id", "name": "Photo", "imageFile": "photo.png", "isVisible": True},
                {"id": "balance-id", "name": TARGET_LAYER_NAME, "isVisible": False,
                 "adjustment": {"kind": "Color Balance"}},
            ],
        }
        (self.source / "manifest.json").write_text(json.dumps(self.manifest))

    def read_layers(self, project):
        return json.loads((project / "manifest.json").read_text())["layers"]

    def test_ev_is_direct_and_invalid_values_are_rejected(self):
        for value in [-2, -0.5, -0.1, 0, 0.5, 2]:
            self.assertEqual(map_exposure_ev(value), {"exposure": value, "offset": 0, "gamma": 1})
        for value in [True, False, "0.5", None, float("nan"), float("inf"),
                      float("-inf"), -2.01, 2.01, 10**500]:
            with self.subTest(value=str(value)), self.assertRaises(ValueError):
                map_exposure_ev(value)

    def test_fixed_ev_projects_preserve_original_and_neutral_controls(self):
        original = (self.source / "manifest.json").read_bytes()
        for index, value in enumerate([-0.5, 0, 0.5]):
            out = create_adjusted_project(
                self.source, self.root / f"fixed-{index}.comp", 0, exposure_ev=value,
            )
            layers = self.read_layers(out)
            layer = layers[-1]
            self.assertEqual(layer["name"], EXPOSURE_LAYER_NAME)
            self.assertEqual(layer["adjustment"]["kind"], "Exposure")
            self.assertEqual(layer["adjustment"]["exposureSettings"],
                             {"exposure": value, "offset": 0, "gamma": 1})
            self.assertTrue(layer["isVisible"])
            self.assertEqual(layer["opacity"], 1)
            self.assertEqual(layer["blendMode"], "Normal")
            self.assertNotIn("imageFile", layer)
            self.assertEqual(str(uuid.UUID(layer["id"])).upper(), layer["id"])
            self.assertEqual(layer["transform"]["size"], [80, 120])
            self.assertEqual(layer["adjustment"]["levels"]["ranges"][0]["gamma"], 1)
            self.assertEqual(layer["adjustment"]["curves"]["channels"][0],
                             [{"x": 0, "y": 0}, {"x": 255, "y": 255}])
            self.assertEqual(layers[0], self.manifest["layers"][0])
            self.assertEqual((out / "images/photo.png").read_bytes(), b"original-image-resource")
        self.assertEqual((self.source / "manifest.json").read_bytes(), original)

    def test_combined_order_and_reuse_replace_ev_instead_of_accumulating(self):
        first = create_adjusted_project(self.source, self.root / "first.comp", 0.25, 0.1, 0.5)
        layers = self.read_layers(first)
        exposure = layers[-2]
        self.assertEqual([layer["name"] for layer in layers[-3:]],
                         [TARGET_LAYER_NAME, EXPOSURE_LAYER_NAME, SATURATION_LAYER_NAME])
        # 即使用户改过 offset/gamma，本工具再次执行也只应用约定的 EV。
        manifest_path = first / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["layers"][-2]["adjustment"]["exposureSettings"] = {
            "exposure": 1, "offset": 0.1, "gamma": 2,
        }
        manifest_path.write_text(json.dumps(manifest))
        second = create_adjusted_project(first, self.root / "second.comp", 0.25, 0.1, -0.1)
        layers = self.read_layers(second)
        matches = [layer for layer in layers if layer["name"] == EXPOSURE_LAYER_NAME]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["id"], exposure["id"])
        self.assertEqual(matches[0]["adjustment"]["exposureSettings"],
                         {"exposure": -0.1, "offset": 0, "gamma": 1})
        self.assertEqual([layer["name"] for layer in layers[-3:]],
                         [TARGET_LAYER_NAME, EXPOSURE_LAYER_NAME, SATURATION_LAYER_NAME])

    def test_omitted_ev_preserves_existing_exposure_and_old_calls_add_none(self):
        old = create_adjusted_project(self.source, self.root / "old.comp", 0, 0.1)
        self.assertNotIn(EXPOSURE_LAYER_NAME, [layer["name"] for layer in self.read_layers(old)])
        first = create_adjusted_project(old, self.root / "first.comp", 0, exposure_ev=0.5)
        layers = self.read_layers(first)
        self.assertEqual(layers[-2]["name"], EXPOSURE_LAYER_NAME)
        exposure = copy.deepcopy(layers[-2])
        second = create_adjusted_project(first, self.root / "second.comp", 0, 0.2)
        self.assertEqual(self.read_layers(second)[-2], exposure)

    def test_invalid_values_and_name_conflicts_stop_before_copy(self):
        output = self.root / "invalid.comp"
        with self.assertRaises(ValueError):
            create_adjusted_project(self.source, output, 0, exposure_ev=3)
        self.assertFalse(output.exists())
        for extra in [
            [{"id": "conflict", "name": EXPOSURE_LAYER_NAME, "adjustment": {"kind": "Curves"}}],
            [{"id": "a", "name": EXPOSURE_LAYER_NAME, "adjustment": {"kind": "Exposure"}},
             {"id": "b", "name": EXPOSURE_LAYER_NAME, "adjustment": {"kind": "Exposure"}}],
            [{"id": "group", "name": EXPOSURE_LAYER_NAME, "isGroup": True,
              "adjustment": {"kind": "Exposure"}}],
        ]:
            manifest = copy.deepcopy(self.manifest)
            manifest["layers"].extend(extra)
            (self.source / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                create_adjusted_project(self.source, output, 0, exposure_ev=0.5)
            self.assertFalse(output.exists())

    def test_cli_accepts_ev_and_rejects_out_of_range(self):
        out = self.root / "cli.comp"
        args = [sys.executable, "compositor_tool.py", "--input", str(self.source),
                "--output", str(out), "--warmth", "0", "--exposure"]
        result = subprocess.run([*args, "-0.5"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("曝光补偿：-0.5 EV", result.stdout)
        self.assertEqual(self.read_layers(out)[-1]["adjustment"]["exposureSettings"]["exposure"], -0.5)
        invalid = self.root / "invalid-cli.comp"
        args[args.index(str(out))] = str(invalid)
        result = subprocess.run([*args, "3"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("曝光参数", result.stderr)
        self.assertFalse(invalid.exists())


if __name__ == "__main__":
    unittest.main()
