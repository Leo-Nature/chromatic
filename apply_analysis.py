"""核对已有分析的来源，将四项基础调整应用到工程副本。"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from compositor_tool import (
    CONTRAST_LAYER_NAME, EXPOSURE_LAYER_NAME, SATURATION_LAYER_NAME, TARGET_LAYER_NAME,
    create_adjusted_project, get_project_image, map_contrast_to_curves, map_exposure_ev,
    map_saturation_adjustment, map_warmth_to_color_balance,
)
from main import validate_analysis


def load_verified_analysis(analysis_path, source_path):
    """将分析绑定到目标工程的唯一图片，拒绝旧的未绑定分析。"""
    analysis_file = Path(analysis_path).expanduser().resolve()
    project, layer, image_file = get_project_image(source_path)
    if not analysis_file.is_file():
        raise ValueError(f"找不到分析 JSON：{analysis_file}")
    analysis_bytes = analysis_file.read_bytes()
    document = json.loads(analysis_bytes)
    result = validate_analysis(document)
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("分析文件缺少 metadata，无法核对照片来源")
    binding = metadata.get("project_source")
    if not isinstance(binding, dict):
        raise ValueError("此分析未绑定工程图片，请用新版统一入口重新分析")
    if binding.get("image_layer_id") != layer["id"] or binding.get("image_file") != layer["imageFile"]:
        raise ValueError("分析记录的图片图层与目标工程不一致，已停止执行")
    recorded_hash = metadata.get("image_sha256")
    if (
        not isinstance(recorded_hash, str)
        or len(recorded_hash) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in recorded_hash)
    ):
        raise ValueError("分析文件缺少有效的 image_sha256 照片指纹")
    actual_hash = hashlib.sha256(image_file.read_bytes()).hexdigest()
    if actual_hash != recorded_hash.lower():
        raise ValueError("工程原图指纹与分析记录不一致，已停止生成工程")
    preview_hash = metadata.get("preview_sha256")
    if (
        not isinstance(preview_hash, str) or len(preview_hash) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in preview_hash)
    ):
        raise ValueError("分析缺少有效的预览指纹，请重新分析")

    source = {
        "analysis_path": str(analysis_file),
        "analysis_sha256": hashlib.sha256(analysis_bytes).hexdigest(),
        "image_path": str(image_file),
        "image_sha256": actual_hash,
        "preview_sha256": preview_hash.lower(),
        "project_path": str(project),
        "image_layer_id": layer["id"],
        "image_file": layer["imageFile"],
    }
    reference = metadata.get("reference")
    if reference is not None:
        result = validate_analysis(document, require_reference=True)
        if not isinstance(reference, dict):
            raise ValueError("参考作品来源必须是有效对象")
        for key in ("image_path", "preview_path"):
            if not isinstance(reference.get(key), str) or not reference[key].strip():
                raise ValueError(f"参考作品来源缺少 {key}")
        for key in ("image_sha256", "preview_sha256"):
            value = reference.get(key)
            if (
                not isinstance(value, str) or len(value) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in value)
            ):
                raise ValueError(f"参考作品来源缺少有效的 {key}")
        # 记录请求时的参考快照，不要求原参考文件此刻仍在原位置。
        source["reference"] = {key: reference[key] for key in (
            "image_path", "image_sha256", "preview_path", "preview_sha256"
        )}
    elif "reference_analysis" in result or metadata.get("analysis_mode") == "reference":
        raise ValueError("参考分析缺少参考作品来源，已停止执行")
    return result, source


def apply_saved_analysis(analysis_path, source_path, output_path):
    """执行四项基础建议，返回工程路径和报告路径。"""
    result, source = load_verified_analysis(analysis_path, source_path)
    requested_output = Path(output_path).expanduser()
    if requested_output.exists() or requested_output.is_symlink():
        raise ValueError("输出工程已经存在，请换一个新的名称")
    output = requested_output.resolve()
    if output.suffix != ".comp":
        raise ValueError("输出工程名称必须以 .comp 结尾")
    # 与工程并列保存，不向 Compositor 工程内部加入自定义报告文件。
    report_path = output.with_suffix(".report.json")
    if report_path.exists() or report_path.is_symlink():
        raise ValueError("对应执行记录已经存在，请换一个新的输出工程名称")

    plan = result["adjustment_plan"]
    warmth = plan["warmth_adjustment"]
    saturation = plan["saturation_adjustment"]
    exposure_ev = plan["exposure_ev"]
    contrast = plan["contrast_adjustment"]
    report = {
        "status": "project_created_pending_visual_review",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "output_project": str(output),
        "photo_source_verified": True,
        "project_photo_verified": True,
        "verification_scope": "原图字节指纹、图层 ID 及资源引用；不代表合成画面或审美已验证",
        "applied_adjustments": {
            "warmth_adjustment": warmth, "saturation_adjustment": saturation,
            "exposure_ev": exposure_ev,
            "contrast_adjustment": contrast,
        },
        "color_balance_settings": map_warmth_to_color_balance(warmth),
        "saturation_settings": {"saturation": map_saturation_adjustment(saturation)},
        "saturation_layer_name": SATURATION_LAYER_NAME,
        "exposure_settings": map_exposure_ev(exposure_ev),
        "exposure_layer_name": EXPOSURE_LAYER_NAME,
        "contrast_curves": map_contrast_to_curves(contrast),
        "contrast_layer_name": CONTRAST_LAYER_NAME,
        "tool_layer_order_bottom_to_top": [
            TARGET_LAYER_NAME, EXPOSURE_LAYER_NAME, CONTRAST_LAYER_NAME, SATURATION_LAYER_NAME,
        ],
        "mapping_rules": {
            "warmth": "round(value * 20)，实验映射，未校准",
            "saturation": "round(value * 100)，范围换算，非视觉百分比",
            "exposure": "EV 档数直接写入 exposure；offset=0，gamma=1；输入限制 [-2, 2]",
            "contrast": "a=value*32；RGB 总曲线 (0,0),(64,64-a),(128,128),(192,192+a),(255,255)；暂定、未校准，非百分比",
        },
        "not_applied_adjustments": {
            name: value for name, value in plan.items()
            if name not in ("warmth_adjustment", "saturation_adjustment", "exposure_ev", "contrast_adjustment")
        },
        "not_applied_reason": "当前方案的四项基础建议均已执行",
        "review_note": "分析来源与工程图片已核对；请对照分析预览检查画面和实际调色效果",
    }
    if "reference_analysis" in result:
        report["analysis_mode"] = "reference"
        report["reference_analysis"] = result["reference_analysis"]
        report["transfer_explanation"] = result["transfer_explanation"]
        report["execution_scope"] = "初步应用冷暖、饱和度、曝光和对比度，不代表完整风格迁移或审美已验收"
    # 报告先序列化为文字，确认内容可保存，再调用已有工程工具。
    report_text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    create_adjusted_project(
        source_path, output, warmth, saturation_adjustment=saturation, exposure_ev=exposure_ev,
        contrast_adjustment=contrast,
    )
    # 检查真正复制出的资源，防止检查后、复制前源文件变化而仍报告成功。
    _, copied_layer, copied_image = get_project_image(output)
    if (
        copied_layer["id"] != source["image_layer_id"]
        or copied_layer["imageFile"] != source["image_file"]
        or hashlib.sha256(copied_image.read_bytes()).hexdigest() != source["image_sha256"]
    ):
        raise ValueError("输出工程图片与分析来源不一致，未生成成功报告；请检查部分输出")
    try:
        with report_path.open("x", encoding="utf-8") as file:
            file.write(report_text + "\n")
    except OSError as error:
        raise ValueError(
            f"工程已生成在 {output}，但执行记录保存失败；请检查该副本和报告路径"
        ) from error
    return output, report_path


def main():
    parser = argparse.ArgumentParser(description="把已存分析的四项基础建议应用到 Compositor 工程")
    parser.add_argument("--analysis", required=True, help="已保存的分析 JSON")
    parser.add_argument("--input", required=True, help="原始 Compositor 工程目录")
    parser.add_argument("--output", required=True, help="新的 .comp 输出工程")
    args = parser.parse_args()
    try:
        output, report_path = apply_saved_analysis(
            args.analysis, args.input, args.output
        )
    except (json.JSONDecodeError, UnicodeError):
        parser.exit(1, "执行失败：分析文件或工程配置不是有效 JSON 文本。\n")
    except (ValueError, OSError) as error:
        parser.exit(1, f"执行失败：{error}\n")
    print(f"已生成工程：{output}")
    print(f"执行记录：{report_path}")
    print("已应用冷暖、饱和度、曝光和对比度建议；请检查实际画面。")
    print("工程图片来源已核对，请对照预览并开关调整图层检查效果。")


if __name__ == "__main__":
    main()
