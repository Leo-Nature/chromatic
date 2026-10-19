"""核对预演、备份、迁移和恢复输出目录；不请求模型，不永久删除材料。"""

import argparse
from collections import defaultdict
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from preview_output_migration import (
    build_migration_plan, file_sha256, historical_reference, proposed_path,
)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    """同目录先写临时文件再替换；原版本由独立备份保存。"""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def tree_snapshot(root):
    """记录文件及空目录，拒绝符号链接，便于完整性核对和保守恢复。"""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("输出目录必须是实际目录，不能是符号链接")
    files, directories = {}, []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"目录含符号链接，请先人工检查：{path}")
        relative = str(path.relative_to(root))
        if path.is_dir():
            directories.append(relative)
        elif path.is_file():
            files[relative] = {"sha256": file_sha256(path), "bytes": path.stat().st_size}
        else:
            raise ValueError(f"不支持的文件类型：{path}")
    return {"files": files, "directories": directories}


def mapped_path(path, root):
    return proposed_path(str(path), root) or Path(path)


def frozen_record(relative):
    return (relative.parts[:2] in (("checks", "migration-previews"), ("checks", "migrations"))
            or "cleanup-records" in relative.parts)


def compile_json_updates(root, snapshot):
    """在内存中按依赖生成新 JSON；已有指纹不匹配、依赖歧义或循环时停止。"""
    root = Path(root)
    raw = {root / name: (root / name).read_bytes()
           for name in snapshot["files"] if name.endswith(".json")}
    documents = {path: json.loads(data) for path, data in raw.items()}
    by_hash = defaultdict(list)
    for path, data in raw.items():
        by_hash[sha(data)].append(path)
    rendered, visiting, order = {}, set(), []

    def render(path):
        if path in rendered:
            return rendered[path]
        if path in visiting:
            raise ValueError(f"JSON 指纹依赖存在循环：{path}")
        visiting.add(path)
        relative = path.relative_to(root)

        def refresh_hash(key, value, node):
            # 优先使用同一对象中的配对路径，不能仅凭旧指纹猜测文件身份。
            paired = "path" if key == "sha256" else key[:-7] + "_path"
            target_text = node.get(paired)
            if isinstance(target_text, str) and Path(target_text).is_absolute():
                target = Path(target_text)
                if target.is_file():
                    actual = sha(raw[target]) if target in raw else file_sha256(target)
                    if actual != value:
                        raise ValueError(f"已有来源指纹不匹配，不自动修复：{path} 的 {key}")
                    return sha(render(target)) if target in raw else value
                if target in raw:
                    raise ValueError(f"依赖文件在扫描后消失：{target}")
            matches = by_hash.get(value, [])
            if not matches:
                return value  # 原图、预览和未关联 JSON 的资源指纹保持不变。
            results = {sha(render(target)) for target in matches}
            if len(results) != 1:
                raise ValueError(f"相同旧指纹对应多个不同的新内容，请人工检查：{path} 的 {key}")
            return results.pop()

        def transform(value, pointer=""):
            if frozen_record(relative):
                return copy.deepcopy(value)
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    field = pointer + "/" + key.replace("~", "~0").replace("/", "~1")
                    if historical_reference(relative, field, "value"):
                        result[key] = copy.deepcopy(item)
                    elif (isinstance(item, str) and len(item) == 64
                          and (key.endswith("_sha256") or key == "sha256")):
                        result[key] = refresh_hash(key, item, value)
                    else:
                        result[key] = transform(item, field)
                return result
            if isinstance(value, list):
                return [transform(item, pointer + "/" + str(i)) for i, item in enumerate(value)]
            if isinstance(value, str):
                return str(mapped_path(value, root)) if Path(value).is_absolute() else value
            return value

        updated = transform(documents[path])
        data = raw[path] if updated == documents[path] else json_bytes(updated)
        if any(part.endswith(".comp") for part in relative.parts[:-1]) and data != raw[path]:
            raise ValueError(f"迁移需要改写工程内部配置，超出当前范围：{path}")
        visiting.remove(path)
        rendered[path] = data
        order.append(path)
        return data

    for path in raw:
        render(path)
    return {str(path.relative_to(root)): rendered[path] for path in order if rendered[path] != raw[path]}


def audit_history(root):
    """复用正式校验入口，只读比较可查看结果、反馈来源和照片导入链。"""
    from app import discover_reports, load_result_view, read_report
    from compositor_tool import get_project_image
    from photo_project import verify_photo_import
    from PIL import Image
    from refine_agent import load_previous_state

    root = Path(root)
    reports = discover_reports(root)
    views, feedback, imports = {}, {}, {}

    def failure(error):
        return {"status": "error", "type": type(error).__name__, "message": str(error)}

    for path in reports:
        key = str(path)
        try:
            view = load_result_view(path)
            views[key] = {"status": "ok", "result": view["result"],
                          "project_error": view["project_error"], "preview_errors": view["preview_errors"],
                          "previews": {k: sha(v) for k, v in view["previews"].items()}}
        except (ValueError, OSError, KeyError, TypeError) as error:
            views[key] = failure(error)
        try:
            report = read_report(path)
            _, _, state, original, reference = load_previous_state(path, report.get("source", {}).get("project_path", ""))
            feedback[key] = {"status": "ok", "plan": state["previous_adjustment_plan"],
                             "original_preview": sha(original), "reference_preview": sha(reference) if reference else None}
        except (ValueError, OSError, KeyError, TypeError) as error:
            feedback[key] = failure(error)

    for path in root.rglob("*.json"):
        if frozen_record(path.relative_to(root)):
            continue
        document = json.loads(path.read_bytes())
        if not isinstance(document, dict):
            continue
        metadata = document.get("metadata", {})
        if "expected_import" in document:
            expected, source = document["expected_import"], document["source_path"]
        elif isinstance(metadata, dict) and "photo_import" in metadata:
            expected, source = metadata["photo_import"], metadata["project_source"]["project_path"]
        else:
            continue
        try:
            source, layer, resource = get_project_image(source)
            with Image.open(resource) as image:
                dimensions = list(image.size)
            verify_photo_import(expected, source, layer, {"image_sha256": file_sha256(resource), "original_dimensions": dimensions})
            imports[str(path)] = {"status": "ok"}
        except (ValueError, OSError, KeyError, TypeError) as error:
            imports[str(path)] = failure(error)
    return {"order": [str(path) for path in reports], "views": views, "feedback": feedback, "imports": imports}


def mapped_audit(before, root):
    return {"order": [str(mapped_path(path, root)) for path in before["order"]],
            **{kind: {str(mapped_path(path, root)): value for path, value in before[kind].items()}
               for kind in ("views", "feedback", "imports")}}


def checked_relative(value):
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or str(path) == ".":
        raise ValueError("恢复清单含不安全的相对路径")
    return path


def write_journal(backup, journal):
    atomic_write(backup / "journal.json", json_bytes(journal))


def ensure_parents(path, root, backup, journal):
    missing = []
    parent = path.parent
    while parent != root and not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        relative = str(directory.relative_to(root))
        if relative not in journal["created_directories"]:
            journal["created_directories"].append(relative)
            write_journal(backup, journal)
        directory.mkdir(exist_ok=False)


def restore_outputs(backup_dir, root):
    """先核对全部现状再恢复；后续新增或改动会阻止覆盖。支持中断后的部分布局。"""
    backup, root = Path(backup_dir).resolve(), Path(root).resolve()
    journal = json.loads((backup / "journal.json").read_bytes())
    if journal.get("version") != 1 or journal["root"] != str(root):
        raise ValueError("备份版本或恢复目标不匹配")
    saved = journal["before"]
    if tree_snapshot(backup / "original-outputs") != saved:
        raise ValueError("备份内容不完整或指纹已变化，停止恢复")
    movements = journal["moves"]
    placements = []
    for movement in movements:
        source = root / checked_relative(movement["source"])
        target = root / checked_relative(movement["target"])
        if source.exists() == target.exists():
            raise ValueError(f"原位置和新位置冲突或同时缺失，停止恢复：{source}")
        placements.append((checked_relative(movement["source"]),
                           checked_relative(movement["target"]) if target.exists() else checked_relative(movement["source"])))

    def located(relative):
        relative = checked_relative(relative)
        for old, current in placements:
            if relative == old or old in relative.parents:
                return str(current / relative.relative_to(old))
        return str(relative)

    current = tree_snapshot(root)
    expected_files = {located(name): info for name, info in saved["files"].items()}
    expected_directories = {located(name) for name in saved["directories"]}
    allowed_directories = expected_directories | set(journal["created_directories"])
    current_directories = set(current["directories"])
    if (set(current["files"]) != set(expected_files)
        or not expected_directories <= current_directories
        or not current_directories <= allowed_directories):
        raise ValueError("输出目录有新增、缺失文件或目录，停止恢复以保护后续工作")
    for old, info in saved["files"].items():
        actual = current["files"][located(old)]["sha256"]
        if actual not in {info["sha256"], journal["after_hashes"][old]}:
            raise ValueError(f"文件在迁移后又被修改，停止恢复：{old}")
    journal["status"] = "restoring"
    write_journal(backup, journal)
    for movement in reversed(movements):
        source, target = root / checked_relative(movement["source"]), root / checked_relative(movement["target"])
        if target.exists():
            os.rename(target, source)
    for old, info in saved["files"].items():
        path = root / checked_relative(old)
        if file_sha256(path) != info["sha256"]:
            atomic_write(path, (backup / "original-outputs" / checked_relative(old)).read_bytes())
    for name in sorted(journal["created_directories"], key=lambda name: len(Path(name).parts), reverse=True):
        directory = root / checked_relative(name)
        if directory.exists():
            directory.rmdir()  # 只移除本工具新建且已空的目录，不删除材料。
    if tree_snapshot(root) != saved or audit_history(root) != journal["history_before"]:
        raise ValueError("恢复后校验不一致，请保留备份和日志进一步检查")
    journal["status"] = "restored"
    write_journal(backup, journal)
    return backup / "journal.json"


def migrate_outputs(root, reviewed_plan, backup_dir, progress=None):
    root = Path(root).expanduser().resolve()
    backup = Path(backup_dir).expanduser().absolute()

    def notify(stage):
        if progress:
            progress(stage)

    reviewed = json.loads(Path(reviewed_plan).read_bytes())
    fresh = build_migration_plan(root)
    if fresh["issues"]:
        raise ValueError(f"目录存在需人工处理的问题：{fresh['issues']}")
    if reviewed.get("root") != str(root) or any(
        reviewed.get(key) != fresh[key] for key in ("moves", "json_reference_review")
    ):
        raise ValueError("预演已过期或文件发生变化，请先重新生成并检查预演")
    if not fresh["moves"]:
        raise ValueError("没有需要迁移的旧条目")
    if (backup.exists() or backup.is_symlink() or backup.resolve().is_relative_to(root)
        or any(parent.is_symlink() for parent in backup.parents)):
        raise ValueError("备份必须是输出目录之外的新目录，不能经过符号链接")
    before = tree_snapshot(root)
    updates = compile_json_updates(root, before)
    history_before = audit_history(root)
    nearest = backup.parent
    while not nearest.exists():
        nearest = nearest.parent
    if shutil.disk_usage(nearest).free < sum(item["bytes"] for item in before["files"].values()) + 64 * 1024 ** 2:
        raise ValueError("可用空间不足以保存完整独立备份")
    backup.mkdir(parents=True, exist_ok=False)
    atomic_write(backup / "reviewed-plan.json", Path(reviewed_plan).read_bytes())
    atomic_write(backup / "fresh-plan.json", json_bytes(fresh))
    shutil.copytree(root, backup / "original-outputs")
    if tree_snapshot(backup / "original-outputs") != before or tree_snapshot(root) != before:
        raise ValueError(f"备份期间文件发生变化；未开始迁移，备份保留在 {backup}")
    staged = backup / "rewritten-json"
    for name, data in updates.items():
        path = staged / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    movements = [{"source": str(Path(item["source"]).relative_to(root)),
                  "target": str(Path(item["target"]).relative_to(root))} for item in fresh["moves"]]
    journal = {
        "version": 1, "status": "backup_verified", "root": str(root),
        "created_at": datetime.now(timezone.utc).isoformat(), "before": before,
        "history_before": history_before, "moves": movements, "created_directories": [],
        "rewritten_json_order": list(updates),
        "after_hashes": {name: sha(updates[name]) if name in updates else info["sha256"] for name, info in before["files"].items()},
        "permanent_deletion": False, "model_called": False,
    }
    write_journal(backup, journal)
    try:
        notify("backup_verified")
        if tree_snapshot(root) != before:
            raise ValueError("开始迁移前目录发生变化")
        journal["status"] = "moving"
        write_journal(backup, journal)
        for movement in movements:
            source, target = root / movement["source"], root / movement["target"]
            ensure_parents(target, root, backup, journal)
            if target.exists() or target.is_symlink():
                raise ValueError(f"目标已存在，停止迁移：{target}")
            journal["current_operation"] = {"move": movement}
            write_journal(backup, journal)
            os.rename(source, target)
            notify("after_move")
        journal["status"] = "rewriting_json"
        write_journal(backup, journal)
        for old, data in updates.items():
            target = mapped_path(root / old, root)
            if file_sha256(target) != before["files"][old]["sha256"]:
                raise ValueError(f"改写前文件已变化：{target}")
            journal["current_operation"] = {"rewrite": old}
            write_journal(backup, journal)
            atomic_write(target, data)
            notify("after_json_write")
        expected_files = {str(mapped_path(root / name, root).relative_to(root)): digest
                          for name, digest in journal["after_hashes"].items()}
        actual = tree_snapshot(root)
        if {name: info["sha256"] for name, info in actual["files"].items()} != expected_files:
            raise ValueError("迁移后的文件清单或指纹不一致")
        after_history = audit_history(root)
        if after_history != mapped_audit(history_before, root):
            raise ValueError("迁移后的历史查看、反馈或导入来源检查与原状态不一致")
        if compile_json_updates(root, actual):
            raise ValueError("迁移后仍有未完成的路径或指纹更新")
        journal["history_after"] = after_history
        journal["status"] = "completed_pending_user_review"
        journal.pop("current_operation", None)
        write_journal(backup, journal)
        notify("verified")
    except Exception as error:
        journal["failure"] = {"type": type(error).__name__, "message": str(error)}
        write_journal(backup, journal)
        try:
            restore_outputs(backup, root)
        except Exception as restore_error:
            raise RuntimeError(f"迁移失败且自动恢复停止：{restore_error}；完整备份和日志：{backup}") from error
        raise RuntimeError(f"迁移失败，已恢复原布局：{error}；备份：{backup}") from error
    return backup / "journal.json"


def main():
    parser = argparse.ArgumentParser(description="备份后迁移输出目录，或按备份恢复；不调用模型")
    sub = parser.add_subparsers(dest="action", required=True)
    apply = sub.add_parser("migrate")
    apply.add_argument("--root", required=True)
    apply.add_argument("--plan", required=True)
    apply.add_argument("--backup", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--root", required=True)
    restore.add_argument("--backup", required=True)
    args = parser.parse_args()
    try:
        if args.action == "migrate":
            def progress(stage):
                if stage in ("backup_verified", "verified"):
                    print(stage, flush=True)
            journal = migrate_outputs(args.root, args.plan, args.backup, progress=progress)
        else:
            journal = restore_outputs(args.backup, args.root)
    except (ValueError, OSError, RuntimeError) as error:
        parser.exit(1, f"操作停止：{error}\n")
    print(f"操作完成；详细日志与恢复信息：{journal}")


if __name__ == "__main__":
    main()
