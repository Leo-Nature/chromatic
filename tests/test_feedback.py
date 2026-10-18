"""反馈调整回归：上轮绑定、真实输入、最终参数语义和失败边界。"""

import base64
import contextlib
import copy
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from apply_analysis import apply_saved_analysis
from main import REFERENCE_FIELDS, request_analysis
from refine_agent import run_refinement
from run_agent import PipelineError, run_pipeline


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromatic-feedback-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source.comp"
        (self.source / "images").mkdir(parents=True)
        Image.new("RGB", (80, 120), "red").save(self.source / "images/photo.png")
        manifest = {
            "format": "com.compositor.project", "width": 80, "height": 120,
            "layers": [
                {"id": "photo-id", "name": "Photo", "imageFile": "photo.png", "isVisible": True},
                {"id": "balance-id", "name": "Test Color Balance", "isVisible": False,
                 "adjustment": {"kind": "Color Balance"}},
            ],
        }
        (self.source / "manifest.json").write_text(json.dumps(manifest))
        self.reference = self.root / "style.jpg"
        Image.new("RGB", (90, 160), "orange").save(self.reference)
        self.original = {
            "analysis": {name: "保持自然" for name in ("exposure", "white_balance", "contrast", "color_tendency")},
            "adjustment_plan": {
                "exposure_ev": -0.1, "contrast_adjustment": 0.15,
                "warmth_adjustment": 0.25, "saturation_adjustment": 0.1,
            },
            "reference_analysis": {name: "参考暖调" for name in REFERENCE_FIELDS},
            "transfer_explanation": "借鉴暖调，保留原图。",
        }
        self.new_result = copy.deepcopy(self.original)
        self.new_result["adjustment_plan"]["warmth_adjustment"] = 0.1
        self.new_result["feedback_explanation"] = "根据用户反馈将冷暖从 0.25 降至 0.1，其余保持；未看到成片。"
        self.previous = self.make_initial_run("initial", self.original, self.reference)

    def make_initial_run(self, name, result, reference=None):
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value=json.dumps(result)
        ), contextlib.redirect_stdout(StringIO()):
            return run_pipeline("借鉴暖调", self.source, self.root / name, reference_path=reference)

    def refine(self, *, report=None, source=None, feedback="太暖了，稍微冷一点，其他保持", output="refined", result=None, side_effect=None):
        with patch("refine_agent.load_api_key", return_value="mock"), patch(
            "refine_agent.request_analysis", return_value=json.dumps(result or self.new_result), side_effect=side_effect,
        ) as request, contextlib.redirect_stdout(StringIO()):
            paths = run_refinement(
                report or self.previous[2], source or self.source,
                feedback, self.root / output,
            )
        return paths, request

    def assert_stops_before_request(self, **kwargs):
        with patch("refine_agent.load_api_key", return_value="mock"), patch(
            "refine_agent.request_analysis", return_value=json.dumps(self.new_result),
        ) as request, contextlib.redirect_stdout(StringIO()):
            with self.assertRaises(PipelineError):
                run_refinement(
                    kwargs.get("report", self.previous[2]), kwargs.get("source", self.source),
                    kwargs.get("feedback", "太暖了，冷一点"),
                    self.root / kwargs.get("output", "refined"),
                )
        request.assert_not_called()
        self.assertFalse((self.root / kwargs.get("output", "refined")).exists())

    def test_request_carries_context_and_honest_observation_scope(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.chat.completions.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="{}", refusal=None))
        ])
        context = {"original_instruction": "暖调", "previous_result": self.original}
        with patch("main.OpenAI", return_value=client):
            request_analysis("original-url", "太暖了", "mock", reference_url="reference-url", feedback_context=context)
        args = client.chat.completions.create.call_args.kwargs
        prompt = args["messages"][0]["content"]
        self.assertIn("没有收到调整后的成片", prompt)
        self.assertIn("不是在上一轮数值上叠加的增量", prompt)
        self.assertIn("feedback_explanation", prompt)
        parts = args["messages"][1]["content"]
        self.assertEqual([p["image_url"]["url"] for p in parts if p["type"] == "image_url"],
                         ["original-url", "reference-url"])
        self.assertIn('"warmth_adjustment": 0.25', parts[-1]["text"])

    def test_full_reference_refinement_uses_saved_previews_and_absolute_plan(self):
        before = {str(p): p.read_bytes() for folder in (self.source, self.previous[0].parent)
                  for p in folder.rglob("*") if p.is_file()}
        self.reference.unlink()  # 复用上轮预览，不重新读取参考原图。
        paths, request = self.refine()
        analysis, project, report_path, result = paths
        self.assertEqual(request.call_count, 1)
        uploaded = base64.b64decode(request.call_args.args[0].split(",", 1)[1])
        reference = base64.b64decode(request.call_args.kwargs["reference_url"].split(",", 1)[1])
        self.assertEqual(uploaded, (analysis.parent / "analysis-preview.jpg").read_bytes())
        self.assertEqual(uploaded, (self.previous[0].parent / "analysis-preview.jpg").read_bytes())
        self.assertEqual(reference, (analysis.parent / "reference-preview.jpg").read_bytes())
        self.assertEqual(reference, (self.previous[0].parent / "reference-preview.jpg").read_bytes())
        self.assertEqual(request.call_args.kwargs["feedback_context"]["previous_result"], self.original)
        document = json.loads(analysis.read_text())
        report = json.loads(report_path.read_text())
        feedback = document["metadata"]["feedback"]
        self.assertEqual(feedback["previous_report_sha256"], hashlib.sha256(self.previous[2].read_bytes()).hexdigest())
        self.assertEqual(feedback["previous_adjustment_plan"], self.original["adjustment_plan"])
        self.assertEqual(document["metadata"]["instruction"], "借鉴暖调")
        self.assertEqual(report["feedback_explanation"], result["feedback_explanation"])
        self.assertEqual(report["feedback"], feedback)
        self.assertEqual(report["parameter_changes"]["warmth_adjustment"]["new"], 0.1)
        for key in ("exposure_ev", "contrast_adjustment", "saturation_adjustment"):
            self.assertEqual(report["parameter_changes"][key]["delta"], 0)
        self.assertEqual(report["color_balance_settings"]["midCyanRed"], 2)
        self.assertEqual(report["source"]["project_path"], str(self.source))
        layers = json.loads((project / "manifest.json").read_text())["layers"]
        self.assertEqual(len(layers), 5)
        self.assertEqual(layers[-4]["adjustment"]["colorBalanceSettings"]["midCyanRed"], 2)
        self.assertEqual(report["not_applied_adjustments"], {})
        for path, data in before.items():
            self.assertEqual(Path(path).read_bytes(), data)
        # 独立重试执行仍保留反馈来源与修改理由。
        _, retry_report = apply_saved_analysis(analysis, self.source, self.root / "retry.comp")
        self.assertEqual(json.loads(retry_report.read_text())["feedback"], feedback)

    def test_automatic_mode_also_refines_without_reference(self):
        basic = {k: self.original[k] for k in ("analysis", "adjustment_plan")}
        initial = self.make_initial_run("automatic", basic)
        result = {**basic, "feedback_explanation": "根据文字反馈保持其他参数，未看到成片。"}
        paths, request = self.refine(report=initial[2], result=result)
        self.assertIsNone(request.call_args.kwargs["reference_url"])
        self.assertNotIn("reference", json.loads(paths[0].read_text())["metadata"])
        self.assertFalse((paths[0].parent / "reference-preview.jpg").exists())

    def test_missing_report_empty_feedback_and_conflicting_output_stop_early(self):
        self.assert_stops_before_request(report=self.root / "missing.json")
        self.assert_stops_before_request(feedback=" ")
        occupied = self.root / "occupied"
        occupied.mkdir()
        with patch("refine_agent.request_analysis") as request, contextlib.redirect_stdout(StringIO()):
            with self.assertRaises(PipelineError):
                run_refinement(self.previous[2], self.source, "冷一点", occupied)
        request.assert_not_called()
        self.assertEqual(list(occupied.iterdir()), [])

    def test_wrong_source_and_old_output_are_not_used_as_original(self):
        self.assert_stops_before_request(source=self.previous[1])
        Image.new("RGB", (80, 120), "blue").save(self.source / "images/photo.png")
        self.assert_stops_before_request()

    def test_report_analysis_hash_or_applied_parameters_mismatch_stops(self):
        original = json.loads(self.previous[2].read_text())
        cases = []
        bad = copy.deepcopy(original)
        bad["source"]["analysis_sha256"] = "0" * 64
        cases.append(bad)
        bad = copy.deepcopy(original)
        bad["applied_adjustments"]["warmth_adjustment"] = 0.9
        cases.append(bad)
        bad = copy.deepcopy(original)
        bad["applied_adjustments"]["exposure_ev"] = True
        cases.append(bad)
        bad = copy.deepcopy(original)
        bad["not_applied_adjustments"] = {"contrast_adjustment": 0.15}
        cases.append(bad)
        for document in cases:
            self.previous[2].write_text(json.dumps(document))
            self.assert_stops_before_request()

    def test_changed_old_tool_or_corrupt_previews_stop_before_api(self):
        manifest_path = self.previous[1] / "manifest.json"
        original = manifest_path.read_bytes()
        manifest = json.loads(original)
        manifest["layers"][-4]["adjustment"]["colorBalanceSettings"]["midCyanRed"] = 99
        manifest_path.write_text(json.dumps(manifest))
        self.assert_stops_before_request()
        manifest["layers"][-3]["adjustment"] = None
        manifest["layers"][-4] = json.loads(original)["layers"][-4]
        manifest_path.write_text(json.dumps(manifest))
        self.assert_stops_before_request()
        manifest_path.write_bytes(original)
        for name in ("analysis-preview.jpg", "reference-preview.jpg"):
            path = self.previous[0].parent / name
            saved = path.read_bytes()
            path.write_bytes(b"corrupt")
            self.assert_stops_before_request()
            path.write_bytes(saved)

    def test_incomplete_old_metadata_stops_before_api(self):
        document = json.loads(self.previous[0].read_text())
        document["metadata"].pop("preview_processing")
        self.previous[0].write_text(json.dumps(document))
        report = json.loads(self.previous[2].read_text())
        report["source"]["analysis_sha256"] = hashlib.sha256(self.previous[0].read_bytes()).hexdigest()
        self.previous[2].write_text(json.dumps(report))
        self.assert_stops_before_request()

    def test_invalid_response_never_executes_and_keeps_previous_results(self):
        cases = []
        missing = copy.deepcopy(self.new_result)
        missing.pop("feedback_explanation")
        cases.append(missing)
        invalid = copy.deepcopy(self.new_result)
        invalid["adjustment_plan"]["warmth_adjustment"] = 2
        cases.append(invalid)
        missing_reference = copy.deepcopy(self.new_result)
        missing_reference.pop("reference_analysis")
        cases.append(missing_reference)
        for index, result in enumerate(cases):
            with patch("refine_agent.apply_saved_analysis") as execute:
                with self.assertRaises(PipelineError):
                    self.refine(result=result, output=f"invalid-{index}")
            execute.assert_not_called()
            self.assertTrue((self.root / f"invalid-{index}/analysis-preview.jpg").exists())
            self.assertFalse((self.root / f"invalid-{index}/analysis.json").exists())
        with self.assertRaises(PipelineError):
            self.refine(side_effect=lambda *a, **kw: "not json", output="bad-json")

    def test_source_changes_during_model_call_preserve_analysis_but_stop_execution(self):
        def change(*args, **kwargs):
            Image.new("RGB", (80, 120), "blue").save(self.source / "images/photo.png")
            return json.dumps(self.new_result)
        with self.assertRaisesRegex(PipelineError, "原图指纹"):
            self.refine(side_effect=change)
        self.assertTrue((self.root / "refined/analysis.json").exists())
        self.assertFalse((self.root / "refined/result.comp").exists())

    def test_feedback_explanation_without_provenance_cannot_be_retried(self):
        paths, _ = self.refine()
        document = json.loads(paths[0].read_text())
        document["metadata"].pop("feedback")
        bad = self.root / "bad-feedback.json"
        bad.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "上一轮来源"):
            apply_saved_analysis(bad, self.source, self.root / "bad.comp")
        self.assertFalse((self.root / "bad.comp").exists())


if __name__ == "__main__":
    unittest.main()
