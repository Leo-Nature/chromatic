"""把 JPEG/PNG 照片导入新的全尺寸 Compositor 原始工程，不调用模型。"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from PIL import Image, ImageCms

from compositor_tool import (
    TARGET_LAYER_NAME, get_project_image, map_warmth_to_color_balance, new_adjustment_layer,
)
from main import load_photo


def verify_photo_import(expected, source_path, image_layer, photo_source):
    """核对上传副本、导入记录与当前工程资源，供页面提交前后复用。"""
    report_path = Path(expected["report_path"])
    report_bytes = report_path.read_bytes()
    if hashlib.sha256(report_bytes).hexdigest() != expected["report_sha256"]:
        raise ValueError("照片导入记录已变化，请重新检查输入")
    if hashlib.sha256(Path(expected["photo_path"]).read_bytes()).hexdigest() != expected["photo_sha256"]:
        raise ValueError("上传原照片副本已变化，请重新检查输入")
    report = json.loads(report_bytes)
    recorded = report["project_image"]
    if (report["output_project"] != str(source_path)
        or report["source"]["image_path"] != expected["photo_path"]
        or report["source"]["image_sha256"] != expected["photo_sha256"]
        or recorded["image_layer_id"] != image_layer["id"]
        or recorded["image_file"] != image_layer["imageFile"]
        or recorded["image_sha256"] != photo_source["image_sha256"]
        or recorded["dimensions"] != photo_source["original_dimensions"]
        or report["manifest_sha256"] != hashlib.sha256((source_path / "manifest.json").read_bytes()).hexdigest()):
        raise ValueError("上传照片、导入记录与原工程来源不一致，请重新检查输入")


def create_photo_project(image_path, output_path):
    """保留输入文件，建立一张图片与中性冷暖层，返回工程及导入记录路径。"""
    requested = Path(output_path).expanduser()
    if requested.suffix != ".comp":
        raise ValueError("输出工程名称必须以 .comp 结尾")
    if requested.exists() or requested.is_symlink():
        raise ValueError("输出工程已经存在，请换一个新名称")
    output = requested.resolve()
    report_path = output.with_suffix(".import.json")
    if report_path.exists() or report_path.is_symlink():
        raise ValueError("导入记录已经存在，请换一个新名称")
    source_path = Path(image_path).expanduser().resolve()
    if any(parent.suffix == ".comp" and (output == parent or parent in output.parents)
           for parent in source_path.parents):
        raise ValueError("新工程不能放在输入照片所在的已有工程内部")

    image, source = load_photo(source_path)
    created = False
    try:
        with image:
            dimensions = list(image.size)  # EXIF 方向校正可能互换宽高；这里不缩放。
            photo_id = str(uuid.uuid4()).upper()
            image_file = f"{photo_id}.png"
            photo_layer = {
                "id": photo_id, "name": "Original Photo", "imageFile": image_file,
                "isGroup": False, "isVisible": True, "opacity": 1, "blendMode": "Normal",
                "transform": {
                    "origin": [0, 0], "size": dimensions, "rotation": 0,
                    "flipX": False, "flipY": False, "sampling": "High quality",
                },
            }
            # 与本地已验收工程使用相同格式版本和画布字段，不依赖本地示例工程。
            manifest = {
                "format": "com.compositor.project", "version": 11,
                "documentID": str(uuid.uuid4()).upper(), "activeLayerID": photo_id,
                "width": image.width, "height": image.height,
                "resolution": 72, "colorSpace": "sRGB", "layers": [photo_layer],
            }
            neutral_settings = map_warmth_to_color_balance(0)
            balance = new_adjustment_layer(manifest, TARGET_LAYER_NAME, {
                "kind": "Color Balance", "colorBalanceSettings": neutral_settings,
            })
            balance["isVisible"] = False
            manifest["layers"].append(balance)

            output.parent.mkdir(parents=True, exist_ok=True)
            output.mkdir(exist_ok=False)
            created = True
            (output / "images").mkdir()
            resource = output / "images" / image_file
            profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            with resource.open("xb") as file:
                # PNG 无损保存规范化像素；不把原相机 EXIF 再带入已校正的资源。
                image.save(file, format="PNG", icc_profile=profile, exif=b"")

        manifest_path = output / "manifest.json"
        with manifest_path.open("x", encoding="utf-8") as file:
            json.dump(manifest, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write("\n")
        project, layer, resource = get_project_image(output)
        with Image.open(resource) as stored:
            stored.load()
            if stored.format != "PNG" or stored.mode != "RGB" or list(stored.size) != dimensions:
                raise ValueError("生成的工程图片格式或尺寸不一致")
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source["image_sha256"]:
            raise ValueError("输入照片在导入期间发生变化，已停止保存成功记录")
        report = {
            "status": "source_project_created_pending_visual_review",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "output_project": str(project),
            "project_image": {
                "image_layer_id": layer["id"], "image_file": layer["imageFile"],
                "image_sha256": hashlib.sha256(resource.read_bytes()).hexdigest(),
                "dimensions": dimensions, "format": "PNG", "color_space": "sRGB",
            },
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "processing": "EXIF 方向校正、RGB/sRGB 转换、全尺寸 PNG；不缩放、不执行调色",
            "neutral_adjustment": {"layer_name": TARGET_LAYER_NAME, "enabled": False,
                                   "color_balance_settings": neutral_settings},
            "verification_scope": "工程结构、图片尺寸及输入指纹已核对；Compositor 加载和视觉效果等待用户检查",
        }
        with report_path.open("x", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write("\n")
        return project, report_path
    except (ValueError, OSError) as error:
        if created:
            raise ValueError(f"{error}；本次部分工程保留在 {output}，请检查后使用新名称重试") from error
        raise


def main():
    parser = argparse.ArgumentParser(description="从 JPEG/PNG 创建全尺寸 Compositor 原始工程，不调用模型")
    parser.add_argument("--input", required=True, help="原照片路径；保持原文件不变")
    parser.add_argument("--output", required=True, help="新的 .comp 工程路径，必须尚不存在")
    args = parser.parse_args()
    try:
        project, report = create_photo_project(args.input, args.output)
    except (ValueError, OSError) as error:
        parser.exit(1, f"照片导入失败：{error}\n")
    print(f"原始工程：{project}\n导入记录：{report}")
    print("已保存全尺寸图片，未调用模型、未执行调色。请在 Compositor 中检查画面和中性图层。")


if __name__ == "__main__":
    main()
