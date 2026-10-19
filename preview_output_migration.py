"""只读生成旧输出目录的分类迁移预演；没有移动、改写历史记录的入口。"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from output_layout import CATEGORIES, classified_relative_path, output_root


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def proposed_path(value, root):
    """按目录边界匹配；也能预演尚未创建的 result_dir，不用字符串替换。"""
    path = Path(value)
    if not path.is_absolute():
        return None
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if not relative.parts or ".." in relative.parts:
        return None
    target = classified_relative_path(relative.parts[0])
    return root / target / Path(*relative.parts[1:]) if target is not None else None


def walk_fields(value, pointer=""):
    """同时检查 JSON 的值与字典键；指纹快照有时把绝对路径作为键。"""
    if isinstance(value, dict):
        for key, item in value.items():
            field = pointer + "/" + key.replace("~", "~0").replace("/", "~1")
            yield field, "key", key
            yield from walk_fields(item, field)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk_fields(item, pointer + "/" + str(index))
    elif isinstance(value, str):
        yield pointer, "value", value


def historical_reference(record, pointer, kind):
    """审计快照与过去的搬移记录保留原值，不能当成当前运行路径直接改写。"""
    return ("cleanup-records" in record.parts
            or "/preservation_snapshot_sha256/" in pointer
            or "/relocations/" in pointer
            or kind == "key")


def build_migration_plan(root):
    root = output_root(root)
    if not root.is_dir() or root.suffix == ".comp":
        raise ValueError("请指定现有 outputs 目录，不能指定 Compositor 工程")
    moves, already_classified, ignored, issues = [], [], [], []
    for source in sorted(root.iterdir()):
        if source.name == ".DS_Store":
            ignored.append(str(source))
            continue
        if source.is_symlink():
            issues.append({"type": "symlink_requires_review", "path": str(source)})
            continue
        if source.name in CATEGORIES:
            if source.is_dir():
                already_classified.append(str(source))
            else:
                issues.append({"type": "category_is_not_directory", "path": str(source)})
            continue
        relative = classified_relative_path(source.name)
        if relative is None:
            issues.append({"type": "unclassified_entry", "path": str(source)})
            continue
        target = root / relative
        # 目标父目录中的符号链接也可能把数据引向另一处，预演中明确报告。
        if any(parent.is_symlink() for parent in [target, *target.parents] if parent.is_relative_to(root)):
            issues.append({"type": "target_symlink_requires_review", "path": str(target)})
        if target.exists() or target.is_symlink():
            issues.append({"type": "target_exists_requires_merge_review", "path": str(target)})
        files = []
        children = sorted(source.rglob("*")) if source.is_dir() else [source]
        for path in children:
            if path.is_symlink():
                issues.append({"type": "symlink_requires_review", "path": str(path)})
                continue
            if path.is_file():
                files.append({"source": str(path), "sha256": file_sha256(path), "bytes": path.stat().st_size})
        moves.append({"source": str(source), "target": str(target), "files": files,
                      "file_count": len(files), "bytes": sum(item["bytes"] for item in files)})

    records = []
    for record in sorted(root.rglob("*.json")):
        relative = record.relative_to(root)
        if relative.parts[:2] == ("checks", "migration-previews"):
            continue  # 预演报告是说明材料，不作为下一次预演的运行依赖。
        if record.is_symlink() or not record.resolve().is_relative_to(root):
            issues.append({"type": "json_symlink_requires_review", "path": str(record)})
            continue
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            issues.append({"type": "unreadable_json", "path": str(record), "detail": str(error)})
            continue
        references, fingerprints = [], []
        for field, kind, value in walk_fields(data):
            if kind == "key" and (value.endswith("sha256") or value == "sha256"):
                fingerprints.append(field)
            target = proposed_path(value, root)
            if target is None:
                continue
            historical = historical_reference(relative, field, kind)
            references.append({"field": field, "kind": kind, "current": value,
                               "proposed": str(target), "currently_exists": Path(value).exists(),
                               "action": "preserve_history" if historical else "review_path_update"})
        if references:
            records.append({"path": str(record), "sha256": file_sha256(record),
                            "references": references, "fingerprint_fields_for_review": fingerprints})
    return {
        "status": "preview_only_requires_review", "created_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root), "historical_files_moved": False, "historical_json_rewritten": False,
        "moves": moves, "already_classified": already_classified, "ignored": ignored,
        "issues": issues, "json_reference_review": records,
        "summary": {"move_count": len(moves), "file_count": sum(item["file_count"] for item in moves),
                    "bytes": sum(item["bytes"] for item in moves), "json_records_to_review": len(records),
                    "path_fields_to_review": sum(ref["action"] == "review_path_update" for rec in records for ref in rec["references"]),
                    "historical_fields_to_preserve": sum(ref["action"] == "preserve_history" for rec in records for ref in rec["references"]),
                    "missing_current_path_fields": sum(ref["action"] == "review_path_update" and not ref["currently_exists"] for rec in records for ref in rec["references"]),
                    "issue_count": len(issues)},
        "limitations": [
            "仅列出路径方案与字段位置，不执行移动、JSON 改写或指纹重算，也没有 --apply 选项。",
            "路径变化会改变 JSON 字节；后续迁移必须按依赖更新引用该 JSON 的指纹。图片和未改写资源的指纹应保持不变。",
            "尚未计算指纹依赖的更新顺序，也未验证所有历史记录有效；缺失路径可能是旧故障或尚未提交任务的未来输出。",
            "历史审计快照保留原值；未知项、冲突和符号链接须人工处理。本报告不是可直接执行的迁移脚本。",
        ],
    }


def markdown_preview(plan):
    root = Path(plan["root"])
    lines = ["# 输出目录迁移预演", "", "**只读预演：没有移动历史文件，也没有改写历史记录。**", "",
             "| 当前位置 | 建议位置 | 文件数 | MiB |", "| --- | --- | ---: | ---: |"]
    for move in plan["moves"]:
        source = Path(move["source"]).relative_to(root)
        target = Path(move["target"]).relative_to(root)
        lines.append(f"| `{source}` | `{target}` | {move['file_count']} | {move['bytes'] / 1024 ** 2:.2f} |")
    lines += ["", "## 引用与指纹", "", json.dumps(plan["summary"], ensure_ascii=False, indent=2), "",
              "完整字段位置、原值及建议值见同名 JSON。需要检查的记录：", ""]
    for record in plan["json_reference_review"]:
        lines.append(f"- `{Path(record['path']).relative_to(root)}`：{len(record['references'])} 处路径；{len(record['fingerprint_fields_for_review'])} 个指纹字段需审查。")
    lines += ["", "## 待处理问题", ""]
    lines += [f"- `{issue['type']}`：{issue['path']}" for issue in plan["issues"]] or ["- 本次扫描未发现未知项、目标冲突或无法读取的 JSON；仍需检查路径引用和指纹依赖。"]
    lines += ["", "## 当前不存在的引用目标", "", "这些路径可能来自旧的缺失工程或尚未提交任务的未来输出；预演不修复或补造文件。", ""]
    missing = [f"- `{Path(record['path']).relative_to(root)}` 的 `{ref['field']}` → `{ref['current']}`"
               for record in plan["json_reference_review"] for ref in record["references"]
               if ref["action"] == "review_path_update" and not ref["currently_exists"]]
    lines += missing or ["- 本次路径字段扫描未发现缺失目标。"]
    lines += ["", "## 当前边界", ""] + [f"- {note}" for note in plan["limitations"]]
    return "\n".join(lines) + "\n"


def save_preview(plan, output):
    output = Path(output).expanduser().absolute()
    if output.suffix != ".json":
        raise ValueError("预演输出必须以 .json 结尾")
    root = Path(plan["root"])
    required_parent = root / "checks" / "migration-previews"
    if not output.resolve().is_relative_to(required_parent):
        raise ValueError("预演只允许保存到 outputs/checks/migration-previews/ 内")
    markdown = output.with_suffix(".md")
    for path in (output, markdown):
        if path.exists() or path.is_symlink():
            raise ValueError("预演文件已经存在，请换一个新名称")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as file:
        json.dump(plan, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write("\n")
    with markdown.open("x", encoding="utf-8") as file:
        file.write(markdown_preview(plan))
    return output, markdown


def main():
    parser = argparse.ArgumentParser(description="输出目录分类迁移只读预演，不移动历史文件")
    parser.add_argument("--root", default="outputs", help="现有输出根目录")
    parser.add_argument("--output", required=True, help="checks/migration-previews/ 下尚不存在的 JSON 路径")
    args = parser.parse_args()
    try:
        plan = build_migration_plan(args.root)
        output, markdown = save_preview(plan, args.output)
    except (ValueError, OSError) as error:
        parser.exit(1, f"预演失败：{error}\n")
    print(json.dumps(plan["summary"], ensure_ascii=False, indent=2))
    print(f"机器可读清单：{output}\n阅读版清单：{markdown}\n只生成预演文件；历史文件没有移动。")


if __name__ == "__main__":
    main()
