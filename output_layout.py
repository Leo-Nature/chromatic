"""集中管理输出目录；只计算路径，不创建或移动文件。"""

from pathlib import Path


CATEGORIES = frozenset({"sources", "inputs", "runs", "experiments", "checks"})
LEGACY_SOURCE_NAMES = frozenset({
    "test-photo-source.comp", "test-photo-source.import.json",
    "photo-import-test.comp", "photo-import-test.import.json",
})
LEGACY_EXPERIMENT_NAMES = frozenset({
    "ai-warm-demo.report.json", "analysis-natural-warm.json", "feedback-run",
    "feedback-warmth-rebound", "reference-model-run", "run-natural-warm", "run-project-source",
    "reference-saturation.comp", "reference-saturation.report.json",
    "reference-exposure.comp", "reference-exposure.report.json",
    "reference-contrast.comp", "reference-contrast.report.json",
    "saturation-tests", "exposure-tests", "contrast-tests",
})


def output_root(path):
    return Path(path).expanduser().resolve()


def input_directory(root, job_id, *, feedback=False):
    return output_root(root) / "inputs" / ("feedback" if feedback else "initial") / job_id


def result_directory(root, name):
    return output_root(root) / "runs" / name


def default_source_project(root):
    """优先新位置；旧工程仍在原处时继续使用旧位置，不自动搬迁。"""
    root = output_root(root)
    preferred = root / "sources" / "test-photo-source.comp"
    legacy = root / "test-photo-source.comp"
    return preferred if preferred.is_dir() or not legacy.is_dir() else legacy


def classified_relative_path(name):
    """只归类已知旧名称；未知材料交给人检查，不猜测其用途。"""
    if name == "ui-inputs":
        return Path("inputs/initial")
    if name == "ui-feedback-inputs":
        return Path("inputs/feedback")
    if name.startswith(("ui-run-", "ui-feedback-")):
        return Path("runs") / name
    if name in LEGACY_SOURCE_NAMES:
        return Path("sources") / name
    if name in LEGACY_EXPERIMENT_NAMES:
        return Path("experiments") / name
    if name in {"viewer-check", "native-export-check", "cleanup-records"}:
        return Path("checks") / name
    return None


def report_candidates(root):
    """限定目录深度，兼容旧布局；不递归扫描工程资源、输入和检查材料。"""
    root = output_root(root)
    paths = set(root.glob("*.report.json"))
    if root.is_dir():
        for child in root.iterdir():
            if (child.is_dir() and not child.is_symlink()
                and child.name not in CATEGORIES and child.suffix != ".comp"):
                paths.update(child.glob("*.report.json"))
    for category, patterns in {
        "runs": ("*/*.report.json",),
        "experiments": ("*.report.json", "*/*.report.json"),
    }.items():
        parent = root / category
        if parent.is_symlink():
            continue
        for pattern in patterns:
            paths.update(parent.glob(pattern))
    return [p for p in paths if p.is_file() and not p.is_symlink()
            and p.resolve().is_relative_to(root)
            and not any(part.endswith(".comp") for part in p.relative_to(root).parts[:-1])]
