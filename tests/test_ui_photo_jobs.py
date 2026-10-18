"""原照片上传入口：来源绑定、预览确认、单次提交及页面真实控件交互。"""

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
from streamlit.testing.v1 import AppTest

from main import REFERENCE_FIELDS
from run_agent import PipelineError
from ui_jobs import (
    feedback_signature, input_signature, job_submitted, photo_input_signature,
    prepare_feedback_job, prepare_photo_job, prepared_previews, submit_feedback_job, submit_job,
)


def show_photo_ui(output_dir):
    from app import render_page
    render_page(output_dir)


def photo_bytes(color, size=(80, 120)):
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class UIPhotoJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromatic-ui-photo-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.outputs = self.root / "outputs"
        self.photo = photo_bytes("red")
        self.reference = photo_bytes("orange", (90, 160))
        self.response = {
            "analysis": {k: "保持自然" for k in ("exposure", "white_balance", "contrast", "color_tendency")},
            "adjustment_plan": {"warmth_adjustment": 0.1, "exposure_ev": 0,
                                "contrast_adjustment": 0.05, "saturation_adjustment": -0.05},
        }

    def prepare(self, reference=False):
        return prepare_photo_job(self.photo, "保持自然", self.outputs,
                                 self.reference if reference else None)

    def response_for(self, reference=False):
        if not reference:
            return dict(self.response)
        return {**self.response, "reference_analysis": {k: "暖调" for k in REFERENCE_FIELDS},
                "transfer_explanation": "借鉴参考暖调，保留原图。"}

    def page(self):
        return AppTest.from_function(show_photo_ui, args=[str(self.outputs)], default_timeout=45)

    def upload(self, page, reference=False):
        page.file_uploader(key="new_photo").set_value(("photo.png", self.photo, "image/png")).run()
        if reference:
            page.file_uploader(key="new_reference").set_value(("style.png", self.reference, "image/png")).run()
        return page

    def test_prepare_saves_raw_import_and_exact_previews_without_api(self):
        with patch("run_agent.load_api_key") as key, patch("run_agent.request_analysis") as request:
            job = self.prepare(reference=True)
        key.assert_not_called()
        request.assert_not_called()
        imported = job["expected_import"]
        self.assertEqual(Path(imported["photo_path"]).read_bytes(), self.photo)
        self.assertEqual(imported["photo_sha256"], hashlib.sha256(self.photo).hexdigest())
        report = json.loads(Path(imported["report_path"]).read_bytes())
        self.assertEqual(report["output_project"], job["source_path"])
        self.assertEqual(report["project_image"]["image_sha256"], job["expected_inputs"]["image_sha256"])
        self.assertEqual(job["input_signature"], photo_input_signature(self.photo, "保持自然", self.reference))
        self.assertEqual(set(prepared_previews(job)), {"original", "reference"})
        self.assertTrue((Path(job["job_dir"]) / "prepared.json").exists())
        self.assertFalse(job_submitted(job))
        self.assertFalse(Path(job["result_dir"]).exists())

    def test_invalid_photo_or_requirements_stop_without_api(self):
        transparent = BytesIO()
        Image.new("RGBA", (20, 30), (1, 2, 3, 0)).save(transparent, format="PNG")
        with patch("run_agent.request_analysis") as request:
            for data, instruction, reference in (
                (None, "自然", None), (b"", "自然", None), (b"broken", "自然", None),
                (transparent.getvalue(), "自然", None), (self.photo, " ", None),
                (self.photo, "自然", b"bad reference"),
            ):
                with self.assertRaises((ValueError, OSError)):
                    prepare_photo_job(data, instruction, self.outputs, reference)
            with patch("ui_jobs.MAX_PHOTO_BYTES", 10), self.assertRaisesRegex(ValueError, "50 MiB"):
                self.prepare()
        request.assert_not_called()
        self.assertFalse(list(self.outputs.glob("ui-run-*")))

    def test_both_modes_submit_once_with_import_provenance_and_same_preview(self):
        for use_reference in (False, True):
            job = self.prepare(reference=use_reference)
            stages = []
            with patch("run_agent.load_api_key", return_value="mock"), patch(
                "run_agent.request_analysis", return_value=json.dumps(self.response_for(use_reference)),
            ) as request, contextlib.redirect_stdout(StringIO()):
                paths = submit_job(job, job["input_signature"], lambda n, _: stages.append(n))
                with self.assertRaisesRegex(ValueError, "已经提交"):
                    submit_job(job, job["input_signature"])
            self.assertEqual(request.call_count, 1)
            self.assertEqual(stages, [1, 2, 3, 4])
            original = base64.b64decode(request.call_args.args[0].split(",", 1)[1])
            self.assertEqual(original, prepared_previews(job)["original"])
            self.assertEqual(original, (paths[0].parent / "analysis-preview.jpg").read_bytes())
            if use_reference:
                sent = base64.b64decode(request.call_args.kwargs["reference_url"].split(",", 1)[1])
                self.assertEqual(sent, prepared_previews(job)["reference"])
            metadata = json.loads(paths[0].read_bytes())["metadata"]
            self.assertEqual(metadata["photo_import"], job["expected_import"])
            report = json.loads(paths[2].read_bytes())
            self.assertEqual(report["source"]["project_path"], job["source_path"])
            self.assertEqual(report["source"]["image_sha256"], job["expected_inputs"]["image_sha256"])

    def test_photo_reference_text_or_input_mode_changes_invalidate_signature(self):
        job = self.prepare(reference=True)
        with patch("run_agent.request_analysis") as request:
            signatures = [
                photo_input_signature(photo_bytes("blue"), "保持自然", self.reference),
                photo_input_signature(self.photo, "更暖", self.reference),
                photo_input_signature(self.photo, "保持自然", None),
                input_signature(job["source_path"], "保持自然", self.reference),
            ]
            for signature in signatures:
                with self.assertRaisesRegex(ValueError, "输入已修改"):
                    submit_job(job, signature)
        request.assert_not_called()
        self.assertFalse(job_submitted(job))

    def test_uploaded_copy_import_record_and_project_changes_stop_before_api(self):
        for kind in ("upload", "import", "manifest", "resource", "reference", "preview"):
            with self.subTest(kind=kind):
                job = self.prepare(reference=True)
                source = Path(job["source_path"])
                paths = {
                    "upload": Path(job["expected_import"]["photo_path"]),
                    "import": Path(job["expected_import"]["report_path"]),
                    "manifest": source / "manifest.json",
                    "resource": source / "images" / job["expected_inputs"]["image_file"],
                    "reference": Path(job["reference_path"]),
                    "preview": Path(job["preview_path"]),
                }
                path = paths[kind]
                if kind in ("import", "manifest"):
                    path.write_bytes(path.read_bytes() + b"\n")
                else:
                    path.write_bytes(b"changed")
                with patch("run_agent.load_api_key") as key, patch("run_agent.request_analysis") as request:
                    with contextlib.redirect_stdout(StringIO()), self.assertRaises((ValueError, PipelineError)):
                        submit_job(job, job["input_signature"])
                key.assert_not_called()
                request.assert_not_called()
                self.assertFalse(Path(job["result_dir"]).exists())

    def test_import_changes_during_api_keep_analysis_but_stop_project(self):
        for key in ("photo_path", "report_path"):
            job = self.prepare()

            def change(*args, **kwargs):
                path = Path(job["expected_import"][key])
                path.write_bytes(path.read_bytes() + b"changed")
                return json.dumps(self.response)

            with patch("run_agent.load_api_key", return_value="mock"), patch(
                "run_agent.request_analysis", side_effect=change,
            ), contextlib.redirect_stdout(StringIO()):
                with self.assertRaisesRegex(PipelineError, "已变化"):
                    submit_job(job, job["input_signature"])
            self.assertTrue((Path(job["result_dir"]) / "analysis.json").exists())
            self.assertFalse((Path(job["result_dir"]) / "result.comp").exists())

    def test_failed_attempt_keeps_imported_source_and_is_not_replayed(self):
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
        self.assertTrue(Path(job["source_path"]).is_dir())
        self.assertEqual(Path(job["expected_import"]["photo_path"]).read_bytes(), self.photo)

    def test_feedback_uses_generated_original_instead_of_edited_output(self):
        job = self.prepare(reference=True)
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value=json.dumps(self.response_for(True)),
        ), contextlib.redirect_stdout(StringIO()):
            initial = submit_job(job, job["input_signature"])
        feedback = "冷暖最终值改为 0.05，其他保持"
        refined = self.response_for(True)
        refined["adjustment_plan"] = {**refined["adjustment_plan"], "warmth_adjustment": 0.05}
        refined["feedback_explanation"] = "根据文字反馈减少暖感，未见成片。"
        feedback_job = prepare_feedback_job(initial[2], feedback, self.outputs)
        self.assertEqual(feedback_job["source_path"], job["source_path"])
        self.assertNotEqual(feedback_job["source_path"], str(initial[1]))
        with patch("refine_agent.load_api_key", return_value="mock"), patch(
            "refine_agent.request_analysis", return_value=json.dumps(refined),
        ), contextlib.redirect_stdout(StringIO()):
            paths = submit_feedback_job(feedback_job, feedback_signature(initial[2], feedback))
        self.assertEqual(json.loads(paths[2].read_bytes())["source"]["project_path"], job["source_path"])

    def test_ui_upload_and_prepare_are_local_and_create_source_only_once(self):
        with patch("run_agent.request_analysis") as request:
            page = self.page().run()
            self.assertEqual(page.radio(key="new_input_mode").value, "上传原照片")
            self.upload(page, reference=True)
            self.assertFalse(page.exception)
            page.button(key="prepare_job").click().run()
            self.assertFalse(page.exception)
            self.assertFalse(page.button(key="submit_job").disabled)
            self.assertEqual(len(page.image), 2)
            sources = list(self.outputs.glob("ui-inputs/*/source.comp"))
            self.assertEqual(len(sources), 1)
            page.run()
            page.button(key="refresh_records").click().run()
            self.assertEqual(list(self.outputs.glob("ui-inputs/*/source.comp")), sources)
        request.assert_not_called()

    def test_ui_photo_reference_text_and_mode_changes_remove_submit(self):
        with patch("run_agent.request_analysis") as request:
            page = self.upload(self.page().run(), reference=True)
            page.button(key="prepare_job").click().run()
            page.file_uploader(key="new_photo").set_value(("other.png", photo_bytes("blue"), "image/png")).run()
            self.assertNotIn("submit_job", [b.key for b in page.button])
            page.button(key="prepare_job").click().run()
            page.file_uploader(key="new_reference").clear().run()
            self.assertNotIn("submit_job", [b.key for b in page.button])
            page.button(key="prepare_job").click().run()
            page.text_area(key="new_instruction").set_value("更暖").run()
            self.assertNotIn("submit_job", [b.key for b in page.button])
            page.button(key="prepare_job").click().run()
            page.radio(key="new_input_mode").set_value("使用已有工程").run()
            self.assertNotIn("submit_job", [b.key for b in page.button])
            self.assertFalse(page.exception)
        request.assert_not_called()

    def test_ui_success_selects_report_refresh_does_not_resubmit_and_feedback_available(self):
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value=json.dumps(self.response_for(True)),
        ) as request, contextlib.redirect_stdout(StringIO()):
            page = self.upload(self.page().run(), reference=True)
            page.button(key="prepare_job").click().run()
            page.button(key="submit_job").click().run()
            self.assertFalse(page.exception)
            self.assertIn("ui-run-", page.selectbox[0].value)
            self.assertTrue(page.button(key="submit_job").disabled)
            self.assertEqual(len(page.metric), 4)
            self.assertIn("prepare_feedback", [b.key for b in page.button])
            self.assertTrue(page.success)
            page.run()
            page.button(key="refresh_records").click().run()
            self.assertEqual(request.call_count, 1)

    def test_ui_invalid_photo_and_missing_photo_have_errors_without_api(self):
        with patch("run_agent.request_analysis") as request:
            page = self.page().run()
            page.button(key="prepare_job").click().run()
            self.assertTrue(page.error)
            page.file_uploader(key="new_photo").set_value(("bad.jpg", b"bad", "image/jpeg")).run()
            page.button(key="prepare_job").click().run()
            self.assertFalse(page.exception)
            self.assertTrue(page.error)
            self.assertNotIn("submit_job", [b.key for b in page.button])
        request.assert_not_called()

    def test_ui_failure_disables_submit_and_refresh_never_retries(self):
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value="not json",
        ) as request, contextlib.redirect_stdout(StringIO()):
            page = self.upload(self.page().run())
            page.button(key="prepare_job").click().run()
            page.button(key="submit_job").click().run()
            self.assertFalse(page.exception)
            self.assertTrue(page.error)
            page.run()
            self.assertTrue(page.button(key="submit_job").disabled)
            self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
