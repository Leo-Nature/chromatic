"""在临时照片工程中测试迁移、反馈指纹链、导入来源和保守恢复。"""

import contextlib
import copy
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from migrate_outputs import audit_history, compile_json_updates, mapped_path, migrate_outputs, restore_outputs, tree_snapshot
from photo_project import create_photo_project
from preview_output_migration import build_migration_plan, file_sha256
from refine_agent import load_previous_state, run_refinement
from run_agent import run_pipeline
from ui_jobs import prepare_feedback_job, prepare_photo_job, submit_job


class OutputMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromatic-migration-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "outputs"
        self.root.mkdir()
        photo = self.base / "original.png"
        Image.new("RGB", (40, 60), "red").save(photo)
        self.source, _ = create_photo_project(photo, self.root / "test-photo-source.comp")
        self.response = {
            "analysis": {key: "保持自然" for key in ("exposure", "white_balance", "contrast", "color_tendency")},
            "adjustment_plan": {"warmth_adjustment": 0.1, "exposure_ev": 0,
                                "contrast_adjustment": 0.1, "saturation_adjustment": -0.1},
        }
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value=json.dumps(self.response)
        ), contextlib.redirect_stdout(StringIO()):
            self.initial = run_pipeline("保持自然", self.source, self.root / "ui-run-first")
        refined = copy.deepcopy(self.response)
        refined["adjustment_plan"]["warmth_adjustment"] = 0.05
        refined["feedback_explanation"] = "根据文字反馈减少暖感。"
        previous = self.initial
        with patch("refine_agent.load_api_key", return_value="mock"), patch(
            "refine_agent.request_analysis", return_value=json.dumps(refined)
        ), contextlib.redirect_stdout(StringIO()):
            for name in ("ui-feedback-first", "feedback-warmth-rebound"):
                previous = run_refinement(previous[2], self.source, "稍微冷一点", self.root / name)
        self.latest = previous
        with patch("ui_jobs.input_directory", return_value=self.root / "ui-inputs/upload"), patch(
            "ui_jobs.result_directory", return_value=self.root / "ui-run-upload"
        ):
            job = prepare_photo_job(photo.read_bytes(), "保持自然", self.root)
        with patch("run_agent.load_api_key", return_value="mock"), patch(
            "run_agent.request_analysis", return_value=json.dumps(self.response)
        ), contextlib.redirect_stdout(StringIO()):
            self.upload = submit_job(job, job["input_signature"])
        with patch("ui_jobs.input_directory", return_value=self.root / "ui-feedback-inputs/pending"), patch(
            "ui_jobs.result_directory", return_value=self.root / "ui-feedback-future"
        ):
            prepare_feedback_job(self.latest[2], "再暖一点", self.root)
        # 保留一个本来就不能作为新版结果读取的早期报告。
        (self.root / "ai-warm-demo.report.json").write_text(json.dumps({"source": {}, "project_photo_verified": False}))
        self.history = self.root / "cleanup-records/check.json"
        self.history.parent.mkdir()
        self.history.write_text(json.dumps({"original_path": str(self.source), "sha256": file_sha256(self.initial[0])}))
        self.export = self.root / "native-export-check/export-record.json"
        self.export.parent.mkdir()
        self.export.write_text(json.dumps({
            "verification": {"report_path": str(self.latest[2]), "report_sha256": file_sha256(self.latest[2])},
            "preservation_snapshot_sha256": {str(self.initial[0]): file_sha256(self.initial[0])},
            "relocations": [{"previous_output_path": str(self.initial[1])}],
        }))
        self.plan = self.root / "checks/migration-previews/test/plan.json"
        self.refresh_plan()
        self.backup = self.base / "backups/first"

    def refresh_plan(self):
        self.plan.parent.mkdir(parents=True, exist_ok=True)
        self.plan.write_text(json.dumps(build_migration_plan(self.root), ensure_ascii=False, indent=2))

    def migrate(self, progress=None):
        with patch("main.OpenAI", side_effect=AssertionError("迁移不得调用模型")):
            return migrate_outputs(self.root, self.plan, self.backup, progress)

    def test_migrates_multiround_feedback_upload_and_preserves_resources_and_audit(self):
        before = tree_snapshot(self.root)
        frozen = self.history.read_bytes()
        old_export = json.loads(self.export.read_text())
        journal = self.migrate()
        self.assertEqual(json.loads(journal.read_bytes())["status"], "completed_pending_user_review")
        self.assertFalse((self.root / "ui-run-first").exists())
        self.assertTrue((self.root / "runs/ui-run-first/result.comp").is_dir())
        latest = mapped_path(self.latest[2], self.root)
        source = mapped_path(self.source, self.root)
        _, _, state, _, _ = load_previous_state(latest, source)
        self.assertEqual(state["previous_adjustment_plan"]["warmth_adjustment"], 0.05)
        analysis = json.loads(mapped_path(self.latest[0], self.root).read_bytes())
        feedback = analysis["metadata"]["feedback"]
        self.assertEqual(feedback["previous_report_sha256"], file_sha256(feedback["previous_report_path"]))
        export = json.loads(mapped_path(self.export, self.root).read_bytes())
        self.assertEqual(export["verification"]["report_sha256"], file_sha256(export["verification"]["report_path"]))
        self.assertEqual(export["preservation_snapshot_sha256"], old_export["preservation_snapshot_sha256"])
        self.assertEqual(export["relocations"], old_export["relocations"])
        self.assertEqual(mapped_path(self.history, self.root).read_bytes(), frozen)
        for name, info in before["files"].items():
            if not name.endswith(".json") or ".comp/" in name:
                self.assertEqual(file_sha256(mapped_path(self.root / name, self.root)), info["sha256"])
        audit = audit_history(self.root)
        self.assertTrue(all(v["status"] == "ok" for v in audit["imports"].values()))
        self.assertEqual(audit["views"][str(self.root / "experiments/ai-warm-demo.report.json")]["status"], "error")
        self.assertEqual(compile_json_updates(self.root, tree_snapshot(self.root)), {})

    def test_completed_migration_restores_exact_original_files_and_empty_directories(self):
        (self.root / "ui-inputs/empty").mkdir()
        self.refresh_plan()
        before = tree_snapshot(self.root)
        self.migrate()
        restore_outputs(self.backup, self.root)
        self.assertEqual(tree_snapshot(self.root), before)
        self.assertEqual(json.loads((self.backup / "journal.json").read_text())["status"], "restored")
        self.assertTrue((self.backup / "rewritten-json").is_dir())

    def test_failure_after_move_automatically_restores_without_model(self):
        before = tree_snapshot(self.root)
        def fail(stage):
            if stage == "after_move":
                raise OSError("模拟移动后失败")
        with self.assertRaisesRegex(RuntimeError, "已恢复原布局"):
            self.migrate(fail)
        self.assertEqual(tree_snapshot(self.root), before)

    def test_failure_after_json_rewrite_automatically_restores_old_hash_chain(self):
        before = tree_snapshot(self.root)
        def fail(stage):
            if stage == "after_json_write":
                raise OSError("模拟改写后失败")
        with self.assertRaisesRegex(RuntimeError, "已恢复原布局"):
            self.migrate(fail)
        self.assertEqual(tree_snapshot(self.root), before)
        load_previous_state(self.latest[2], self.source)

    def test_interrupted_move_can_be_manually_recovered_using_journal(self):
        before = tree_snapshot(self.root)
        def interrupt(stage):
            if stage == "after_move":
                raise SystemExit("模拟进程中断")
        with self.assertRaises(SystemExit):
            self.migrate(interrupt)
        restore_outputs(self.backup, self.root)
        self.assertEqual(tree_snapshot(self.root), before)

    def test_changed_preview_or_new_target_stops_before_backup_and_mutation(self):
        self.initial[0].write_bytes(self.initial[0].read_bytes() + b"\n")
        before = tree_snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "预演已过期"):
            self.migrate()
        self.assertEqual(tree_snapshot(self.root), before)
        self.assertFalse(self.backup.exists())

    def test_target_conflict_stops_before_mutation(self):
        (self.root / "runs/ui-run-first").mkdir(parents=True)
        before = tree_snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "人工处理"):
            self.migrate()
        self.assertEqual(tree_snapshot(self.root), before)

    def test_bad_existing_pin_is_not_repaired_by_migration(self):
        report = json.loads(self.initial[2].read_bytes())
        report["source"]["analysis_sha256"] = "0" * 64
        self.initial[2].write_text(json.dumps(report))
        self.refresh_plan()
        before = tree_snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "已有来源指纹不匹配"):
            self.migrate()
        self.assertEqual(tree_snapshot(self.root), before)
        self.assertFalse(self.backup.exists())

    def test_restore_refuses_to_overwrite_subsequent_edits(self):
        self.migrate()
        path = mapped_path(self.initial[0], self.root)
        path.write_bytes(path.read_bytes() + b"\n")
        before = tree_snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "又被修改"):
            restore_outputs(self.backup, self.root)
        self.assertEqual(tree_snapshot(self.root), before)

    def test_restore_refuses_new_tasks_and_incomplete_backup(self):
        self.migrate()
        path = self.root / "runs/new-task/new.txt"
        path.parent.mkdir()
        path.write_text("新的任务，不可丢弃")
        before = tree_snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "新增"):
            restore_outputs(self.backup, self.root)
        self.assertEqual(tree_snapshot(self.root), before)

    def test_restore_checks_backup_hashes_before_touching_current_files(self):
        self.migrate()
        backup_file = self.backup / "original-outputs" / self.initial[0].relative_to(self.root)
        backup_file.write_text("损坏的备份")
        before = tree_snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "备份内容不完整"):
            restore_outputs(self.backup, self.root)
        self.assertEqual(tree_snapshot(self.root), before)

    def test_symbolic_links_and_unknown_material_stop_without_changes(self):
        (self.root / "link").symlink_to(self.base / "original.png")
        with self.assertRaisesRegex(ValueError, "人工处理"):
            self.migrate()
        self.assertFalse(self.backup.exists())

    def test_ambiguous_hash_only_dependency_stops_before_backup(self):
        # 相同旧内容位于运行记录与冻结的审计记录，迁移后两者内容会不同。
        data = json.dumps({"project_path": str(self.source)})
        runtime = self.initial[0].parent / "custom.json"
        frozen = self.history.parent / "same.json"
        runtime.write_text(data)
        frozen.write_text(data)
        record = json.loads(self.export.read_text())
        record["custom_sha256"] = file_sha256(runtime)
        self.export.write_text(json.dumps(record))
        self.refresh_plan()
        before = tree_snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "多个不同的新内容"):
            self.migrate()
        self.assertEqual(tree_snapshot(self.root), before)
        self.assertFalse(self.backup.exists())

    def test_backup_cannot_be_placed_inside_outputs(self):
        self.backup = self.root / "backup"
        before = tree_snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "输出目录之外"):
            self.migrate()
        self.assertEqual(tree_snapshot(self.root), before)

    def test_restore_refuses_missing_original_empty_directory(self):
        empty = self.root / "ui-inputs/empty"
        empty.mkdir()
        self.refresh_plan()
        self.migrate()
        mapped_path(empty, self.root).rmdir()
        before = tree_snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "新增、缺失"):
            restore_outputs(self.backup, self.root)
        self.assertEqual(tree_snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
