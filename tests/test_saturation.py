"""基础饱和度执行测试；用小工程验证写入、隔离和兼容性。"""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from compositor_tool import SATURATION_LAYER_NAME, create_adjusted_project, map_saturation_adjustment


class SaturationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromatic-saturation-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source.comp"
        (self.source / "images").mkdir(parents=True)
        (self.source / "images/photo.png").write_bytes(b"resource-kept-verbatim")
        self.manifest = {
            "format": "com.compositor.project", "width": 80, "height": 120,
            "layers": [
                {"id": "photo-id", "name": "Photo", "imageFile": "photo.png", "isVisible": True},
                {"id": "balance-id", "name": "Test Color Balance", "isVisible": False,
                 "adjustment": {"kind": "Color Balance"}},
            ],
        }
        (self.source / "manifest.json").write_text(json.dumps(self.manifest))

    def test_range_mapping_and_invalid_values(self):
        for value, expected in [(-1, -100), (-0.3, -30), (0, 0), (0.1, 10), (1, 100)]:
            self.assertEqual(map_saturation_adjustment(value), expected)
        for value in [True, False, "0.1", None, float("nan"), float("inf"), -1.1, 1.1, 10**500]:
            with self.subTest(value=str(value)), self.assertRaises(ValueError):
                map_saturation_adjustment(value)

    def test_positive_zero_negative_layers_preserve_source_and_resources(self):
        original = (self.source / "manifest.json").read_bytes()
        for index, value in enumerate([-0.3, 0, 0.3]):
            out = create_adjusted_project(self.source, self.root / f"test-{index}.comp", 0, value)
            manifest = json.loads((out / "manifest.json").read_text())
            layer = manifest["layers"][-1]
            self.assertEqual(layer["name"], SATURATION_LAYER_NAME)
            self.assertEqual(layer["adjustment"]["kind"], "Hue/Saturation")
            self.assertEqual(layer["adjustment"]["saturation"], round(value * 100))
            self.assertEqual(layer["adjustment"]["hue"], 0)
            self.assertEqual(layer["adjustment"]["lightness"], 0)
            self.assertFalse(layer["adjustment"]["colorize"])
            self.assertTrue(layer["isVisible"])
            self.assertNotIn("imageFile", layer)
            self.assertEqual(str(uuid.UUID(layer["id"])).upper(), layer["id"])
            self.assertEqual(layer["transform"]["size"], [80, 120])
            self.assertEqual(manifest["layers"][0], self.manifest["layers"][0])
            self.assertEqual((out / "images/photo.png").read_bytes(), b"resource-kept-verbatim")
        self.assertEqual((self.source / "manifest.json").read_bytes(), original)

    def test_old_warmth_only_call_does_not_add_saturation_layer(self):
        out = create_adjusted_project(self.source, self.root / "old.comp", 0.5)
        layers = json.loads((out / "manifest.json").read_text())["layers"]
        self.assertEqual(len(layers), 2)
        self.assertEqual(layers[1]["adjustment"]["colorBalanceSettings"]["midCyanRed"], 10)

    def test_reusing_tool_layer_replaces_value_without_adding_layers(self):
        first = create_adjusted_project(self.source, self.root / "first.comp", 0.2, 0.4)
        first_layer = json.loads((first / "manifest.json").read_text())["layers"][-1]
        second = create_adjusted_project(first, self.root / "second.comp", 0.1, -0.2)
        layers = json.loads((second / "manifest.json").read_text())["layers"]
        matches = [layer for layer in layers if layer["name"] == SATURATION_LAYER_NAME]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["id"], first_layer["id"])
        self.assertEqual(matches[0]["adjustment"]["saturation"], -20)
        self.assertIs(matches[0], layers[-1])

    def test_invalid_parameter_or_reserved_name_collision_creates_no_output(self):
        output = self.root / "invalid.comp"
        with self.assertRaises(ValueError):
            create_adjusted_project(self.source, output, 0, 2)
        self.assertFalse(output.exists())
        for duplicate in (False, True):
            manifest = copy.deepcopy(self.manifest)
            conflict = {"id": "conflict", "name": SATURATION_LAYER_NAME,
                        "adjustment": {"kind": "Curves"}}
            manifest["layers"].append(conflict)
            if duplicate:
                manifest["layers"].append({**conflict, "id": "another"})
            (self.source / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                create_adjusted_project(self.source, output, 0, 0.1)
            self.assertFalse(output.exists())

    def test_cli_accepts_saturation(self):
        out = self.root / "cli.comp"
        result = subprocess.run([
            sys.executable, "compositor_tool.py", "--input", str(self.source),
            "--output", str(out), "--warmth", "0", "--saturation", "-0.3",
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("饱和度参数", result.stdout)
        self.assertEqual(json.loads((out / "manifest.json").read_text())["layers"][-1]["adjustment"]["saturation"], -30)


if __name__ == "__main__":
    unittest.main()
