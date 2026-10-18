"""页面反馈：所选版本绑定、单次提交、进度、失败和连续版本。"""

import base64
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

from main import REFERENCE_FIELDS
from run_agent import PipelineError, run_pipeline
from ui_jobs import feedback_signature, job_submitted, prepare_feedback_job, submit_feedback_job


def show_feedback_ui(output_dir):
    from app import render_page
    render_page(output_dir)


class UIFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromatic-ui-feedback-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.outputs = self.root / "outputs"
        self.source = self.root / "source.comp"
        (self.source / "images").mkdir(parents=True)
        Image.new("RGB", (80, 120), "red").save(self.source / "images/photo.png")
        (self.source / "manifest.json").write_text(json.dumps({
            "format": "com.compositor.project", "width": 80, "height": 120,
            "layers": [
                {"id": "photo-id", "name": "Photo", "imageFile": "photo.png", "isVisible": True},
                {"id": "balance-id", "name": "Test Color Balance", "isVisible": False,
                 "adjustment": {"kind": "Color Balance"}},
            ],
        }))
        self.reference = self.root / "reference.jpg"
        Image.new("RGB", (90, 160), "orange").save(self.reference)
        self.initial_result = {
            "analysis": {k: "保持自然" for k in ("exposure", "white_balance", "contrast", "color_tendency")},
            "adjustment_plan": {"warmth_adjustment": 0.1, "exposure_ev": 0,
                                "contrast_adjustment": 0.05, "saturation_adjustment": -0.05},
            "reference_analysis": {k: "暖调" for k in REFERENCE_FIELDS},
            "transfer_explanation": "借鉴暖色，保留原图。",
        }
        self.initial = self.make_initial("initial", reference=True)
        self.response = copy.deepcopy(self.initial_result)
        self.response["adjustment_plan"]["warmth_adjustment"] = 0.05
        self.response["feedback_explanation"] = "冷暖最终值改为 0.05，其他保持；未看到成片。"
        self.feedback = "将冷暖最终值调整为 0.05，其他参数保持不变。"

    def make_initial(self, name, reference):
        result = copy.deepcopy(self.initial_result)
        if not reference:
            result.pop("reference_analysis")
            result.pop("transfer_explanation")
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value=json.dumps(result),
        ), contextlib.redirect_stdout(StringIO()):
            return run_pipeline("借鉴暖调", self.source, self.outputs / name,
                                self.reference if reference else None)

    def prepare(self, report=None):
        return prepare_feedback_job(report or self.initial[2], self.feedback, self.outputs)

    def page(self):
        return AppTest.from_function(show_feedback_ui, args=[str(self.outputs)], default_timeout=45)

    def test_prepare_derives_original_and_snapshot_without_api(self):
        with patch("refine_agent.load_api_key") as key, patch("refine_agent.request_analysis") as request:
            job = self.prepare()
        key.assert_not_called()
        request.assert_not_called()
        self.assertEqual(job["source_path"], str(self.source))
        self.assertEqual(job["previous_report"], str(self.initial[2]))
        state = job["expected_previous"]["state"]
        self.assertEqual(state["previous_report_sha256"], hashlib.sha256(self.initial[2].read_bytes()).hexdigest())
        self.assertEqual(state["previous_adjustment_plan"], self.initial_result["adjustment_plan"])
        self.assertTrue((Path(job["job_dir"]) / "prepared.json").is_file())
        self.assertFalse(job_submitted(job))
        self.assertFalse(Path(job["result_dir"]).exists())

    def test_invalid_feedback_partial_history_and_output_inside_project_stop(self):
        with patch("refine_agent.request_analysis") as request:
            for text in ("", " "):
                with self.assertRaises(ValueError):
                    prepare_feedback_job(self.initial[2], text, self.outputs)
            with self.assertRaises(OSError):
                prepare_feedback_job(self.root / "missing.json", self.feedback, self.outputs)
            with self.assertRaises(ValueError):
                prepare_feedback_job(self.initial[2], self.feedback, self.initial[1] / "inside")
            report = json.loads(self.initial[2].read_bytes())
            report["not_applied_adjustments"] = {"contrast_adjustment": 0.05}
            self.initial[2].write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "四项"):
                self.prepare()
        request.assert_not_called()

    def test_submit_once_four_stages_saved_context_and_original_preserved(self):
        before = {p: p.read_bytes() for directory in (self.source, self.initial[0].parent)
                  for p in directory.rglob("*") if p.is_file()}
        job = self.prepare()
        stages = []
        with patch("refine_agent.load_api_key", return_value="mock"), patch(
            "refine_agent.request_analysis", return_value=json.dumps(self.response),
        ) as request, contextlib.redirect_stdout(StringIO()):
            paths = submit_feedback_job(job, job["input_signature"], lambda n, _: stages.append(n))
            with self.assertRaisesRegex(ValueError, "已经提交"):
                submit_feedback_job(job, job["input_signature"])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(stages, [1, 2, 3, 4])
        self.assertEqual(request.call_args.kwargs["feedback_context"]["previous_result"], self.initial_result)
        sent = base64.b64decode(request.call_args.args[0].split(",", 1)[1])
        self.assertEqual(sent, (self.initial[0].parent / "analysis-preview.jpg").read_bytes())
        self.assertEqual(sent, (paths[0].parent / "analysis-preview.jpg").read_bytes())
        reference = base64.b64decode(request.call_args.kwargs["reference_url"].split(",", 1)[1])
        self.assertEqual(reference, (paths[0].parent / "reference-preview.jpg").read_bytes())
        metadata = json.loads(paths[0].read_bytes())["metadata"]
        self.assertEqual(metadata["confirmed_previous"], job["expected_previous"])
        report = json.loads(paths[2].read_bytes())
        self.assertEqual(report["source"]["project_path"], str(self.source))
        self.assertEqual(report["applied_adjustments"], self.response["adjustment_plan"])
        self.assertEqual(report["color_balance_settings"]["midCyanRed"], 1)
        for key in ("exposure_ev", "contrast_adjustment", "saturation_adjustment"):
            self.assertEqual(report["parameter_changes"][key]["delta"], 0)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)

    def test_changed_text_or_selected_report_requires_new_preparation(self):
        job = self.prepare()
        with patch("refine_agent.request_analysis") as request:
            for signature in (feedback_signature(self.initial[2], "更暖"),
                              feedback_signature(self.root / "other.report.json", self.feedback)):
                with self.assertRaisesRegex(ValueError, "版本或反馈已修改"):
                    submit_feedback_job(job, signature)
        request.assert_not_called()
        self.assertFalse(job_submitted(job))

    def test_report_analysis_previews_and_project_drift_stop_before_api(self):
        for path in (self.initial[2], self.initial[0], self.source / "manifest.json",
                     self.initial[1] / "manifest.json", self.initial[0].parent / "reference-preview.jpg",
                     self.source / "images/photo.png", self.initial[1] / "images/photo.png"):
            with self.subTest(path=path):
                job = self.prepare()
                saved = path.read_bytes()
                try:
                    path.write_bytes(saved + b"\n") if path.suffix == ".json" else path.write_bytes(b"changed")
                    with patch("refine_agent.load_api_key") as key, patch("refine_agent.request_analysis") as request:
                        with contextlib.redirect_stdout(StringIO()), self.assertRaises(PipelineError):
                            submit_feedback_job(job, job["input_signature"])
                    key.assert_not_called()
                    request.assert_not_called()
                    self.assertFalse(Path(job["result_dir"]).exists())
                finally:
                    path.write_bytes(saved)

    def test_configuration_changes_during_call_keep_analysis_stop_execution(self):
        for path in (self.source / "manifest.json", self.initial[1] / "manifest.json"):
            job = self.prepare()
            saved = path.read_bytes()

            def change(*args, **kwargs):
                path.write_bytes(saved + b"\n")
                return json.dumps(self.response)

            try:
                with patch("refine_agent.load_api_key", return_value="mock"), patch(
                    "refine_agent.request_analysis", side_effect=change,
                ), contextlib.redirect_stdout(StringIO()):
                    with self.assertRaisesRegex(PipelineError, "工程配置在模型请求期间发生变化"):
                        submit_feedback_job(job, job["input_signature"])
                self.assertTrue((Path(job["result_dir"]) / "analysis.json").is_file())
                self.assertFalse((Path(job["result_dir"]) / "result.comp").exists())
            finally:
                path.write_bytes(saved)

    def test_failed_submission_is_consumed_without_automatic_retry(self):
        job = self.prepare()
        with patch("refine_agent.load_api_key", return_value="mock"), patch(
            "refine_agent.request_analysis", return_value="not json",
        ) as request, contextlib.redirect_stdout(StringIO()):
            with self.assertRaises(PipelineError):
                submit_feedback_job(job, job["input_signature"])
            with self.assertRaisesRegex(ValueError, "已经提交"):
                submit_feedback_job(job, job["input_signature"])
        self.assertEqual(request.call_count, 1)
        self.assertTrue(job_submitted(job))
        self.assertTrue((Path(job["result_dir"]) / "analysis-preview.jpg").exists())
        self.assertFalse((Path(job["result_dir"]) / "result.comp").exists())

    def test_automatic_mode_and_continuous_feedback_keep_original_source(self):
        initial = self.make_initial("automatic", reference=False)
        response = {k: v for k, v in self.response.items() if k not in ("reference_analysis", "transfer_explanation")}
        for _ in range(2):
            job = self.prepare(initial[2])
            with patch("refine_agent.load_api_key", return_value="mock"), patch(
                "refine_agent.request_analysis", return_value=json.dumps(response),
            ) as request, contextlib.redirect_stdout(StringIO()):
                paths = submit_feedback_job(job, job["input_signature"])
            self.assertIsNone(request.call_args.kwargs["reference_url"])
            self.assertFalse((paths[0].parent / "reference-preview.jpg").exists())
            report = json.loads(paths[2].read_bytes())
            self.assertEqual(report["feedback"]["previous_report_path"], str(initial[2]))
            self.assertEqual(report["source"]["project_path"], str(self.source))
            initial = paths

    def test_ui_prepare_refresh_and_edits_never_call_model(self):
        with patch("refine_agent.request_analysis") as request:
            page = self.page().run()
            page.text_area(key="feedback_text").set_value(self.feedback).run()
            page.button(key="prepare_feedback").click().run()
            self.assertFalse(page.exception)
            self.assertFalse(page.button(key="submit_feedback").disabled)
            page.button(key="refresh_records").click().run()
            page.text_area(key="feedback_text").set_value("更暖").run()
            self.assertNotIn("submit_feedback", [button.key for button in page.button])
            page.text_area(key="feedback_text").set_value(" ").run()
            page.button(key="prepare_feedback").click().run()
            self.assertTrue(page.error)
        request.assert_not_called()

    def test_ui_switching_versions_invalidates_confirmation(self):
        other = self.make_initial("other", reference=True)
        with patch("refine_agent.request_analysis") as request:
            page = self.page().run()
            page.text_area(key="feedback_text").set_value(self.feedback).run()
            page.button(key="prepare_feedback").click().run()
            page.selectbox[0].select(str(self.initial[2])).run()
            self.assertFalse(page.exception)
            self.assertNotIn("submit_feedback", [button.key for button in page.button])
            page.selectbox[0].select(str(other[2])).run()
            self.assertNotIn("submit_feedback", [button.key for button in page.button])
        request.assert_not_called()

    def test_ui_success_selects_new_report_and_refresh_does_not_resubmit(self):
        with patch("refine_agent.load_api_key", return_value="mock"), patch(
            "refine_agent.request_analysis", return_value=json.dumps(self.response),
        ) as request, contextlib.redirect_stdout(StringIO()):
            page = self.page().run()
            page.text_area(key="feedback_text").set_value(self.feedback).run()
            page.button(key="prepare_feedback").click().run()
            page.button(key="submit_feedback").click().run()
            self.assertFalse(page.exception)
            self.assertIn("ui-feedback-", page.selectbox[0].value)
            self.assertEqual([m.value for m in page.metric], ["+0.05", "+0.00 EV", "+0.05", "-0.05"])
            self.assertTrue(page.success)
            self.assertNotIn("submit_feedback", [button.key for button in page.button])
            self.assertTrue(page.table)  # 新版本反馈前后对照。
            page.run()
            page.button(key="refresh_records").click().run()
            self.assertEqual(request.call_count, 1)

    def test_ui_failure_shows_stage_disables_submit_and_requires_check_again(self):
        with patch("refine_agent.load_api_key", return_value="mock"), patch(
            "refine_agent.request_analysis", return_value="not json",
        ) as request, contextlib.redirect_stdout(StringIO()):
            page = self.page().run()
            page.text_area(key="feedback_text").set_value(self.feedback).run()
            page.button(key="prepare_feedback").click().run()
            page.button(key="submit_feedback").click().run()
            self.assertFalse(page.exception)
            self.assertTrue(page.error)
            self.assertIn("校验并保存反馈方案失败", page.error[0].value)
            self.assertTrue(page.button(key="submit_feedback").disabled)
            page.run()
            self.assertEqual(request.call_count, 1)
            page.button(key="prepare_feedback").click().run()
            self.assertFalse(page.button(key="submit_feedback").disabled)
            self.assertEqual(request.call_count, 1)

    def test_ui_partial_history_or_missing_preview_disables_feedback(self):
        path = self.initial[2]
        saved = path.read_bytes()
        report = json.loads(saved)
        plan = report["applied_adjustments"]
        report["applied_adjustments"] = {"warmth_adjustment": 0.1}
        report["not_applied_adjustments"] = {k: v for k, v in plan.items() if k != "warmth_adjustment"}
        path.write_text(json.dumps(report))
        with patch("refine_agent.request_analysis") as request:
            page = self.page().run()
            self.assertFalse(page.exception)
            self.assertNotIn("prepare_feedback", [button.key for button in page.button])
            path.write_bytes(saved)
            (self.initial[0].parent / "reference-preview.jpg").unlink()
            page.run()
            self.assertFalse(page.exception)
            self.assertNotIn("prepare_feedback", [button.key for button in page.button])
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
