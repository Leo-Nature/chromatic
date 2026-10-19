"""只读核对已登记的 Compositor PNG，返回网页预览与原文件字节。"""

import hashlib
from io import BytesIO
import json
from pathlib import Path
import re

from PIL import Image, PngImagePlugin

from compositor_tool import get_project_image
from refine_agent import load_previous_state, required_path


PREVIEW_MAX_EDGE = 1600
MAX_EXPORT_PIXELS = 80_000_000
EXPORT_STATUSES = {"native_export_created_pending_visual_review", "native_export_user_reviewed"}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def inside_output(path, root):
    path = Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("导出记录、结果工程和成片必须位于本项目输出目录内")
    return path


def check_hash(data, expected, label):
    if not isinstance(expected, str) or sha256(data) != expected.lower():
        raise ValueError(f"{label}指纹与导出记录不一致，请重新核对该版本的导出")


def find_export_record(root, report_file):
    """限定检查目录，兼容旧记录位置；多份匹配时拒绝猜选。"""
    paths = {root / "native-export-check/export-record.json"}
    paths.update((root / "checks").glob("*/export-record.json"))
    paths.update((root / "checks/exports").glob("*/export-record.json"))
    matches = []
    for path in sorted(paths):
        if not path.exists():
            continue
        path = inside_output(path, root)
        data = path.read_bytes()
        document = json.loads(data)
        if not isinstance(document, dict) or not isinstance(document.get("verification"), dict):
            raise ValueError("导出记录缺少有效的版本核对信息")
        recorded_report = required_path(document["verification"], "report_path")
        if recorded_report == report_file:
            matches.append((path, data, document))
    if len(matches) > 1:
        raise ValueError("有多份导出记录指向当前版本，请先确认要保留哪一份")
    return matches[0] if matches else None


def make_png_preview(data, dimensions):
    """生成内存中的显示副本；不修改下载字节，不向磁盘写预览。"""
    with Image.open(BytesIO(data)) as image:
        if image.format != "PNG":
            raise ValueError("成片实际格式不是 PNG")
        if image.size != tuple(dimensions):
            raise ValueError("成片像素尺寸与工程画布或导出记录不一致")
        if image.width * image.height > MAX_EXPORT_PIXELS:
            raise ValueError("成片超出当前 8000 万像素的显示范围")
        if image.mode not in {"RGB", "RGBA"}:
            raise ValueError("当前成片查看支持 RGB 或 RGBA PNG")
        image.verify()
    with Image.open(BytesIO(data)) as image:
        image.load()
        if image.getexif().get(274, 1) != 1:
            raise ValueError("成片带有额外方向变换，请核对 Compositor 导出方向")
        mode = image.mode
        profile = image.info.get("icc_profile")
        srgb = image.info.get("srgb")
        image.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.Resampling.LANCZOS)
        metadata = PngImagePlugin.PngInfo()
        if isinstance(srgb, int) and 0 <= srgb <= 3:
            metadata.add(b"sRGB", bytes([srgb]))
        buffer = BytesIO()
        image.save(buffer, format="PNG", pnginfo=metadata, icc_profile=profile, exif=b"")
        return buffer.getvalue(), list(image.size), mode


def load_export_for_report(output_dir, report_path):
    """无登记时返回 None；有记录但校验失败时抛错，绝不回退到其他版本图片。"""
    root = Path(output_dir).resolve()
    report_file = inside_output(report_path, root)
    try:
        match = find_export_record(root, report_file)
        if match is None:
            return None
        record_file, record_bytes, record = match
        if record.get("status") not in EXPORT_STATUSES:
            raise ValueError("导出记录尚未达到可查看状态")
        verification = record["verification"]
        report_bytes = report_file.read_bytes()
        check_hash(report_bytes, verification.get("report_sha256"), "执行报告")
        original_project = required_path(verification, "original_project")
        result, _, state, _, _ = load_previous_state(report_file, original_project)
        if state["previous_report_sha256"] != sha256(report_bytes):
            raise ValueError("执行报告在核对期间变化，请刷新页面")
        project = inside_output(required_path(verification, "source_project"), root)
        if str(project) != state["previous_output_project"]:
            raise ValueError("导出记录对应的结果工程不是当前版本")
        manifest_bytes = (project / "manifest.json").read_bytes()
        check_hash(manifest_bytes, verification.get("source_manifest_sha256"), "工程配置")
        manifest = json.loads(manifest_bytes)
        _, layer, resource = get_project_image(project)
        if (layer["id"] != verification.get("image_layer_id")
            or resource != required_path(verification, "image_resource")
            or verification.get("adjustment_plan") != result["adjustment_plan"]):
            raise ValueError("导出记录的图片来源或调色方案与当前版本不一致")
        check_hash(resource.read_bytes(), verification.get("image_resource_sha256"), "工程原照片")
        output = verification.get("output")
        if not isinstance(output, dict) or output.get("format") != "PNG":
            raise ValueError("导出记录缺少有效的 PNG 成片信息")
        png_path = inside_output(required_path(output, "path"), root)
        data = png_path.read_bytes()
        check_hash(data, output.get("sha256"), "成片")
        dimensions = [manifest["width"], manifest["height"]]
        if output.get("dimensions") != dimensions or output.get("bytes") != len(data):
            raise ValueError("成片的尺寸或文件大小与导出记录不一致")
        preview, preview_dimensions, mode = make_png_preview(data, dimensions)
        if output.get("mode") != mode:
            raise ValueError("成片的颜色模式与导出记录不一致")
        # 下载使用本次已核对的字节快照，不在点击时改读另一个路径。
        if (record_file.read_bytes() != record_bytes or report_file.read_bytes() != report_bytes
            or (project / "manifest.json").read_bytes() != manifest_bytes):
            raise ValueError("导出记录或工程在准备预览期间变化，请刷新页面")
        name = re.sub(r"[^A-Za-z0-9_-]", "_", report_file.parent.name)
        return {
            "record_path": record_file, "png_path": png_path,
            "png_bytes": data, "preview_bytes": preview,
            "dimensions": dimensions, "preview_dimensions": preview_dimensions,
            "sha256": sha256(data), "report_sha256": sha256(report_bytes),
            "download_name": f"chromatic-{name}-{sha256(data)[:8]}.png",
            "user_reviewed": record["status"] == "native_export_user_reviewed",
        }
    except (ValueError, OSError, KeyError, TypeError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError(f"成片核对失败：{error}") from error
