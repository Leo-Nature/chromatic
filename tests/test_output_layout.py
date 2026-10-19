"""分类目录与预演：覆盖新旧共存、引用边界、冲突与历史文件保护。"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from app import discover_reports
from output_layout import default_source_project, input_directory, result_directory
from preview_output_migration import build_migration_plan, proposed_path, save_preview


class OutputLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromatic-layout-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "outputs"
        self.root.mkdir()

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def snapshot(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*") if p.is_file()}

    def test_reports_from_both_layouts_sorted_without_scanning_materials(self):
        accepted = []
        for index, name in enumerate(("old.report.json", "old-run/result.report.json",
                                      "runs/ui-run-new/result.report.json",
                                      "experiments/reference.report.json",
                                      "experiments/old-run/result.report.json")):
            accepted.append(self.write(name, {"created_at": f"2026-10-19T0{index}:00:00+00:00"}))
        for name in ("sources/photo.comp/hidden.report.json", "inputs/initial/hidden.report.json",
                     "checks/hidden.report.json", "runs/task/result.comp/hidden.report.json",
                     "runs/task/deeper/hidden.report.json", "old.comp/hidden.report.json"):
            self.write(name, {"created_at": "9999"})
        external = self.base / "external.report.json"
        external.write_text('{"created_at":"9999"}')
        (self.root / "link.report.json").symlink_to(external)
        before = self.snapshot()
        self.assertEqual(discover_reports(self.root), list(reversed(accepted)))
        self.assertEqual(before, self.snapshot())

    def test_path_helpers_do_not_create_directories_and_default_keeps_old_source(self):
        legacy = self.root / "test-photo-source.comp"
        legacy.mkdir()
        self.assertEqual(default_source_project(self.root), legacy)
        preferred = self.root / "sources/test-photo-source.comp"
        preferred.mkdir(parents=True)
        self.assertEqual(default_source_project(self.root), preferred)
        self.assertEqual(input_directory(self.root, "abc"), self.root / "inputs/initial/abc")
        self.assertEqual(input_directory(self.root, "abc", feedback=True), self.root / "inputs/feedback/abc")
        self.assertEqual(result_directory(self.root, "ui-run-abc"), self.root / "runs/ui-run-abc")
        self.assertFalse((self.root / "inputs").exists())
        self.assertFalse((self.root / "runs").exists())

    def test_preview_records_paths_and_fingerprints_without_mutating_history(self):
        source = self.write("test-photo-source.comp/manifest.json", {"layers": []})
        analysis = self.write("ui-run-old/analysis.json", {"metadata": {"image_path": str(source)}})
        self.write("ui-run-old/result.report.json", {"source": {"analysis_path": str(analysis),
                   "analysis_sha256": hashlib.sha256(analysis.read_bytes()).hexdigest()}})
        self.write("ui-inputs/abc/prepared.json", {"result_dir": str(self.root / "ui-run-future")})
        before = self.snapshot()
        plan = build_migration_plan(self.root)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(plan["historical_files_moved"])
        self.assertFalse(plan["historical_json_rewritten"])
        self.assertEqual(plan["issues"], [])
        self.assertEqual(plan["summary"]["move_count"], 3)
        records = {Path(r["path"]).name: r for r in plan["json_reference_review"]}
        self.assertTrue(records["result.report.json"]["fingerprint_fields_for_review"])
        future = records["prepared.json"]["references"][0]
        self.assertFalse(future["currently_exists"])
        self.assertEqual(plan["summary"]["missing_current_path_fields"], 1)
        self.assertEqual(future["proposed"], str(self.root / "runs/ui-run-future"))
        output, markdown = save_preview(plan, self.root / "checks/migration-previews/first.json")
        for path, digest in before.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)
        self.assertTrue(markdown.is_file())
        again = build_migration_plan(self.root)
        self.assertEqual(again["json_reference_review"], plan["json_reference_review"])
        self.assertEqual(again["moves"], plan["moves"])
        self.assertEqual(json.loads(output.read_text())["status"], "preview_only_requires_review")

    def test_missing_or_unknown_entries_and_target_conflicts_are_exposed(self):
        (self.root / "ui-run-old").mkdir()
        (self.root / "runs/ui-run-old").mkdir(parents=True)
        self.write("unclassified.json", {})
        (self.root / "broken.json").write_text("not JSON")
        (self.root / "linked").symlink_to(self.base)
        plan = build_migration_plan(self.root)
        types = {i["type"] for i in plan["issues"]}
        self.assertTrue({"target_exists_requires_merge_review", "unclassified_entry",
                         "unreadable_json", "symlink_requires_review"} <= types)

    def test_operational_references_and_historical_snapshots_are_distinguished(self):
        old = str(self.root / "test-photo-source.comp/manifest.json")
        self.write("native-export-check/export-record.json", {
            "verification": {"source_project": old},
            "preservation_snapshot_sha256": {old: "historical fingerprint"},
            "relocations": [{"previous_output_path": old}],
        })
        self.write("cleanup-records/cleanup.json", {"original_path": old})
        plan = build_migration_plan(self.root)
        actions = [ref["action"] for record in plan["json_reference_review"] for ref in record["references"]]
        self.assertEqual(actions.count("review_path_update"), 1)
        self.assertEqual(actions.count("preserve_history"), 3)

    def test_path_boundary_and_unknown_path_are_not_guessed(self):
        self.assertIsNone(proposed_path(str(self.base / "outputs-other/ui-run-old"), self.root))
        self.assertIsNone(proposed_path("outputs/ui-run-old", self.root))
        self.assertIsNone(proposed_path(str(self.root / "unknown/file"), self.root))
        self.assertIsNone(proposed_path(str(self.root / "ui-run-old/../other"), self.root))

    def test_existing_categories_remain_and_report_writes_cannot_overwrite_history(self):
        self.write("runs/task/analysis.json", {})
        plan = build_migration_plan(self.root)
        self.assertEqual(plan["moves"], [])
        self.assertIn(str(self.root / "runs"), plan["already_classified"])
        target = self.root / "checks/migration-previews/first.json"
        target.parent.mkdir(parents=True)
        target.with_suffix(".md").write_text("keep")
        with self.assertRaisesRegex(ValueError, "已经存在"):
            save_preview(plan, target)
        self.assertFalse(target.exists())
        with self.assertRaisesRegex(ValueError, "只允许"):
            save_preview(plan, self.root / "history.json")
        self.assertFalse((self.root / "history.json").exists())

    def test_invalid_root_is_rejected(self):
        with self.assertRaises(ValueError):
            build_migration_plan(self.root / "missing")
        project = self.root / "source.comp"
        project.mkdir()
        with self.assertRaises(ValueError):
            build_migration_plan(project)


if __name__ == "__main__":
    unittest.main()
