"""只读查看器测试：版本绑定、缺失文件、控件交互及准确打开工程。"""

import contextlib
import copy
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from streamlit.testing.v1 import AppTest

from app import discover_reports, load_result_view, open_result_project, report_label
from main import REFERENCE_FIELDS
from refine_agent import run_refinement
from run_agent import run_pipeline


def show_viewer(output_dir):
    # AppTest 会在独立脚本上下文中执行此函数，因此在函数内部导入页面。
    from app import render_page
    render_page(output_dir)


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromatic-viewer-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.outputs = self.root / "outputs"
        self.source = self.root / "source.comp"
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
        self.reference = self.root / "reference.jpg"
        Image.new("RGB", (90, 160), "orange").save(self.reference)
        self.initial_result = {
            "analysis": {key: "原照片分析" for key in ("exposure", "white_balance", "contrast", "color_tendency")},
            "adjustment_plan": {"warmth_adjustment": 0.25, "exposure_ev": -0.1,
                                "contrast_adjustment": 0.15, "saturation_adjustment": 0.1},
            "reference_analysis": {key: "参考作品特征" for key in REFERENCE_FIELDS},
            "transfer_explanation": "借鉴暖调，保留原图细节。",
        }
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value=json.dumps(self.initial_result)
        ), contextlib.redirect_stdout(StringIO()):
            self.initial = run_pipeline("借鉴暖调", self.source, self.outputs / "initial", self.reference)
        result = copy.deepcopy(self.initial_result)
        result["adjustment_plan"]["warmth_adjustment"] = 0.1
        result["feedback_explanation"] = "根据文字反馈减少暖感，其他保持；未见成片。"
        with patch("refine_agent.load_api_key", return_value="mock"), patch(
            "refine_agent.request_analysis", return_value=json.dumps(result)
        ), contextlib.redirect_stdout(StringIO()):
            self.feedback = run_refinement(self.initial[2], self.source, "稍微冷一点", self.outputs / "feedback")

    def page(self):
        return AppTest.from_function(show_viewer, args=[str(self.outputs)], default_timeout=45)

    def test_discovery_and_unique_labels_newest_first(self):
        paths = discover_reports(self.outputs)
        self.assertEqual(paths, [self.feedback[2], self.initial[2]])
        labels = [report_label(path, self.outputs) for path in paths]
        self.assertNotEqual(labels[0], labels[1])
        self.assertIn("反馈调整", labels[0])
        self.assertIn("feedback/result.report.json", labels[0])

    def test_read_only_previews_and_feedback_are_bound_to_record(self):
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.root.rglob("*") if p.is_file()}
        with patch("main.OpenAI", side_effect=AssertionError("查看器不得请求模型")):
            view = load_result_view(self.feedback[2])
        self.assertEqual(view["report"]["applied_adjustments"]["warmth_adjustment"], 0.1)
        self.assertEqual(view["source"]["feedback"]["text"], "稍微冷一点")
        self.assertEqual(view["previews"]["original"], (self.feedback[0].parent / "analysis-preview.jpg").read_bytes())
        self.assertEqual(view["previews"]["reference"], (self.feedback[0].parent / "reference-preview.jpg").read_bytes())
        self.assertIsNone(view["project_error"])
        self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in self.root.rglob("*") if p.is_file()})

    def test_selection_and_refresh_keep_correct_version_without_api_or_open(self):
        with patch("main.OpenAI", side_effect=AssertionError("不得请求模型")), patch("app.open_result_project") as opener:
            page = self.page().run()
            self.assertFalse(page.exception)
            self.assertEqual(page.selectbox[0].value, str(self.feedback[2]))
            self.assertEqual(page.metric[0].value, "+0.10")
            self.assertEqual(len(page.metric), 4)
            self.assertEqual(len(page.image), 2)
            page.selectbox[0].select(str(self.initial[2])).run()
            self.assertFalse(page.exception)
            self.assertEqual(page.metric[0].value, "+0.25")
            page.button(key="refresh_records").click().run()
            self.assertEqual(page.selectbox[0].value, str(self.initial[2]))
            opener.assert_not_called()

    def test_button_opens_only_the_selected_record(self):
        with patch("main.OpenAI", side_effect=AssertionError("不得请求模型")), patch("app.open_result_project") as opener:
            page = self.page().run()
            page.selectbox[0].select(str(self.initial[2])).run()
            page.button(key="open_project").click().run()
            self.assertFalse(page.exception)
            opener.assert_called_once_with(str(self.initial[2]))
            self.assertTrue(page.success)

    def test_open_rechecks_project_and_uses_argument_list(self):
        app_path = self.root / "Compositor.app"
        app_path.mkdir()
        with patch("app.COMPOSITOR_APP", app_path), patch("app.sys.platform", "darwin"), patch("app.subprocess.run") as run:
            open_result_project(self.feedback[2])
            self.assertEqual(run.call_args.args[0], ["/usr/bin/open", "-a", str(app_path), str(self.feedback[1])])
            self.assertNotIn("shell", run.call_args.kwargs)
            Image.new("RGB", (80, 120), "blue").save(self.feedback[1] / "images/photo.png")
            run.reset_mock()
            with self.assertRaises(ValueError):
                open_result_project(self.feedback[2])
            run.assert_not_called()

    def test_missing_preview_or_result_does_not_crash_and_disables_open(self):
        (self.feedback[0].parent / "reference-preview.jpg").unlink()
        (self.feedback[1] / "manifest.json").unlink()
        page = self.page().run()
        self.assertFalse(page.exception)
        self.assertEqual(page.metric[0].value, "+0.10")
        self.assertTrue(page.button(key="open_project").disabled)
        self.assertGreaterEqual(len(page.warning), 2)

    def test_source_mismatch_stops_display_without_open(self):
        Image.new("RGB", (80, 120), "blue").save(self.source / "images/photo.png")
        with patch("app.open_result_project") as opener:
            page = self.page().run()
        self.assertFalse(page.exception)
        self.assertTrue(page.error)
        self.assertEqual(len(page.metric), 0)
        opener.assert_not_called()

    def test_historical_partial_execution_is_not_presented_as_all_applied(self):
        document = json.loads(self.initial[2].read_text())
        plan = self.initial_result["adjustment_plan"]
        document["applied_adjustments"] = {"warmth_adjustment": plan["warmth_adjustment"]}
        document["not_applied_adjustments"] = {k: v for k, v in plan.items() if k != "warmth_adjustment"}
        self.initial[2].write_text(json.dumps(document))
        page = self.page().run()
        page.selectbox[0].select(str(self.initial[2])).run()
        self.assertFalse(page.exception)
        self.assertEqual(page.metric[1].value, "未执行")

    def test_empty_and_malformed_records_have_readable_states(self):
        empty = self.root / "empty"
        page = AppTest.from_function(show_viewer, args=[str(empty)], default_timeout=15).run()
        self.assertFalse(page.exception)
        self.assertTrue(page.info)
        self.feedback[2].write_text("not json")
        page = self.page().run()
        page.selectbox[0].select(str(self.feedback[2])).run()
        self.assertFalse(page.exception)
        self.assertTrue(page.error)


if __name__ == "__main__":
    unittest.main()
