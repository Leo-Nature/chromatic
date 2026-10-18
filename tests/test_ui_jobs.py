"""页面初次调色：预览绑定、单次提交、阶段通知与控件重运行。"""

import base64
import contextlib
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
from run_agent import PipelineError
from ui_jobs import (
    input_signature, job_submitted, prepare_job, prepared_previews, submit_job,
)


def show_analysis_ui(output_dir):
    from app import render_page
    render_page(output_dir)


class UIJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromatic-ui-job-test-")
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
        self.reference = self.root / "style.png"
        Image.new("RGB", (90, 160), "orange").save(self.reference)
        self.result = {
            "analysis": {k: "保持自然" for k in ("exposure", "white_balance", "contrast", "color_tendency")},
            "adjustment_plan": {"exposure_ev": 0, "contrast_adjustment": 0.1,
                                "warmth_adjustment": 0.1, "saturation_adjustment": -0.1},
        }

    def prepare(self, reference=False):
        return prepare_job(self.source, "保持自然", self.outputs,
                           self.reference.read_bytes() if reference else None)

    def page(self):
        return AppTest.from_function(show_analysis_ui, args=[str(self.outputs)], default_timeout=45)

    def test_prepare_previews_without_api_or_original_changes(self):
        before = {str(p): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        with patch("run_agent.load_api_key") as key, patch("run_agent.request_analysis") as request:
            job = self.prepare(reference=True)
        key.assert_not_called()
        request.assert_not_called()
        previews = prepared_previews(job)
        self.assertEqual(set(previews), {"original", "reference"})
        self.assertEqual(hashlib.sha256(previews["original"]).hexdigest(), job["expected_inputs"]["preview_sha256"])
        self.assertEqual(hashlib.sha256(Path(job["reference_path"]).read_bytes()).hexdigest(),
                         hashlib.sha256(self.reference.read_bytes()).hexdigest())
        self.assertFalse(job_submitted(job))
        for path, data in before.items():
            self.assertEqual(Path(path).read_bytes(), data)

    def test_invalid_inputs_never_request_model(self):
        with patch("run_agent.request_analysis") as request:
            for source, instruction, reference in [
                (self.source, " ", None), ("", "自然", None),
                (self.root / "missing.comp", "自然", None),
                (self.source, "自然", b"bad image"), (self.source, "自然", b""),
                (self.source, "自然", b"x" * (20 * 1024 * 1024 + 1)),
            ]:
                with self.assertRaises((ValueError, OSError)):
                    prepare_job(source, instruction, self.outputs, reference)
            with self.assertRaises(ValueError):
                prepare_job(self.source, "自然", self.source / "inside")
        request.assert_not_called()

    def test_submits_once_with_exact_confirmed_previews_and_four_stages(self):
        for use_reference in (False, True):
            job = self.prepare(reference=use_reference)
            response = dict(self.result)
            if use_reference:
                response.update(reference_analysis={k: "暖调" for k in REFERENCE_FIELDS}, transfer_explanation="借鉴暖色，保留细节。")
            stages = []
            with patch("run_agent.load_api_key", return_value="mock"), patch(
                "run_agent.request_analysis", return_value=json.dumps(response),
            ) as request, contextlib.redirect_stdout(StringIO()):
                paths = submit_job(job, job["input_signature"], lambda n, text: stages.append(n))
                with self.assertRaisesRegex(ValueError, "已经提交"):
                    submit_job(job, job["input_signature"])
            self.assertEqual(request.call_count, 1)
            self.assertEqual(stages, [1, 2, 3, 4])
            sent = base64.b64decode(request.call_args.args[0].split(",", 1)[1])
            self.assertEqual(sent, Path(job["preview_path"]).read_bytes())
            self.assertEqual(sent, (paths[0].parent / "analysis-preview.jpg").read_bytes())
            if use_reference:
                reference = base64.b64decode(request.call_args.kwargs["reference_url"].split(",", 1)[1])
                self.assertEqual(reference, Path(job["reference_preview_path"]).read_bytes())
                self.assertEqual(reference, (paths[0].parent / "reference-preview.jpg").read_bytes())
            metadata = json.loads(paths[0].read_text())["metadata"]
            self.assertEqual(metadata["confirmed_inputs"], job["expected_inputs"])
            self.assertEqual(json.loads(paths[2].read_text())["applied_adjustments"], self.result["adjustment_plan"])

    def test_draft_edits_preview_tampering_or_source_changes_stop_before_api(self):
        job = self.prepare(reference=True)
        with patch("run_agent.request_analysis") as request:
            with self.assertRaisesRegex(ValueError, "输入已修改"):
                submit_job(job, input_signature(self.source, "更冷一点", self.reference.read_bytes()))
            Path(job["preview_path"]).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "预览指纹"):
                submit_job(job, job["input_signature"])
        request.assert_not_called()
        for change in ("manifest", "photo", "reference"):
            job = self.prepare(reference=True)
            path = (self.source / "manifest.json" if change == "manifest" else
                    self.source / "images/photo.png" if change == "photo" else Path(job["reference_path"]))
            original = path.read_bytes()
            if change == "manifest":
                path.write_bytes(original + b"\n")
            else:
                Image.new("RGB", (80, 120), "blue").save(path, format="PNG")
            with patch("run_agent.request_analysis") as request, contextlib.redirect_stdout(StringIO()):
                with self.assertRaisesRegex(PipelineError, "已发生变化"):
                    submit_job(job, job["input_signature"])
            request.assert_not_called()
            self.assertFalse(Path(job["result_dir"]).exists())
            path.write_bytes(original)

    def test_failed_attempt_keeps_previews_and_cannot_silently_retry(self):
        job = self.prepare()
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value="not json",
        ) as request, contextlib.redirect_stdout(StringIO()):
            with self.assertRaises(PipelineError):
                submit_job(job, job["input_signature"])
            with self.assertRaisesRegex(ValueError, "已经提交"):
                submit_job(job, job["input_signature"])
        self.assertEqual(request.call_count, 1)
        self.assertTrue(job_submitted(job))
        self.assertTrue((Path(job["result_dir"]) / "analysis-preview.jpg").exists())
        self.assertFalse((Path(job["result_dir"]) / "result.comp").exists())

    def test_manifest_changes_during_request_keep_analysis_but_stop_execution(self):
        job = self.prepare()

        def change(*args, **kwargs):
            path = self.source / "manifest.json"
            path.write_bytes(path.read_bytes() + b"\n")
            return json.dumps(self.result)

        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", side_effect=change,
        ), contextlib.redirect_stdout(StringIO()):
            with self.assertRaisesRegex(PipelineError, "配置在分析期间发生变化"):
                submit_job(job, job["input_signature"])
        self.assertTrue((Path(job["result_dir"]) / "analysis.json").exists())
        self.assertFalse((Path(job["result_dir"]) / "result.comp").exists())

    def test_ui_editing_preparing_and_refresh_do_not_call_model(self):
        with patch("run_agent.request_analysis") as request:
            page = self.page().run()
            page.text_input(key="new_source").set_value(str(self.source)).run()
            page.text_area(key="new_instruction").set_value("保持自然").run()
            self.assertFalse(page.exception)
            self.assertEqual(len(page.button), 2)  # 只准备输入与刷新，无提交按钮。
            page.button(key="prepare_job").click().run()
            self.assertFalse(page.exception)
            self.assertFalse(page.button(key="submit_job").disabled)
            page.button(key="refresh_records").click().run()
            page.text_area(key="new_instruction").set_value("稍微冷一点").run()
            self.assertNotIn("submit_job", [button.key for button in page.button])
        request.assert_not_called()

    def test_ui_success_selects_new_report_and_reruns_do_not_resubmit(self):
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value=json.dumps(self.result),
        ) as request, contextlib.redirect_stdout(StringIO()):
            page = self.page().run()
            page.text_input(key="new_source").set_value(str(self.source)).run()
            page.button(key="prepare_job").click().run()
            page.button(key="submit_job").click().run()
            self.assertFalse(page.exception)
            self.assertEqual(request.call_count, 1)
            self.assertTrue(page.button(key="submit_job").disabled)
            self.assertEqual(page.metric[0].value, "+0.10")
            self.assertIn("ui-run-", page.selectbox[0].value)
            self.assertTrue(page.success)
            page.run()
            page.button(key="refresh_records").click().run()
            self.assertEqual(request.call_count, 1)

    def test_ui_validation_and_request_failure_show_errors_without_replay(self):
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value="not json",
        ) as request, contextlib.redirect_stdout(StringIO()):
            page = self.page().run()
            page.text_input(key="new_source").set_value("\0").run()
            self.assertFalse(page.exception)
            self.assertTrue(page.error)
            request.assert_not_called()
            page.text_input(key="new_source").set_value(str(self.source)).run()
            page.text_area(key="new_instruction").set_value(" ").run()
            page.button(key="prepare_job").click().run()
            self.assertTrue(page.error)
            request.assert_not_called()
            page.text_area(key="new_instruction").set_value("自然").run()
            page.button(key="prepare_job").click().run()
            page.button(key="submit_job").click().run()
            self.assertFalse(page.exception)
            self.assertTrue(page.error)
            page.run()
            self.assertTrue(page.button(key="submit_job").disabled)
            self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
