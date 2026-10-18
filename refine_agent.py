"""根据上一轮执行记录和用户反馈，从同一原工程生成新版本。"""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path

from openai import APIConnectionError, APIStatusError
from PIL import Image

from apply_analysis import apply_saved_analysis, load_verified_analysis
from compositor_tool import (
    CONTRAST_LAYER_NAME, EXPOSURE_LAYER_NAME, SATURATION_LAYER_NAME, TARGET_LAYER_NAME,
    get_project_image, map_contrast_to_curves, map_exposure_ev,
    map_saturation_adjustment, map_warmth_to_color_balance,
)
from main import MODEL, PARAMETER_RANGES, load_api_key, request_analysis, save_result, validate_analysis
from run_agent import PipelineError, describe_error


def required_path(document, key):
    """只接受记录中的非空路径，避免缺失字段被误当成当前目录。"""
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"上一轮记录缺少有效的 {key}")
    return Path(value).expanduser().resolve()


def read_verified_preview(path, expected_hash):
    """读取上轮已发送的 JPEG 原字节，不重新缩放或压缩。"""
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError("上一轮预览指纹不一致，已停止反馈请求")
    with Image.open(BytesIO(data)) as image:
        if image.format != "JPEG" or max(image.size) > 1600:
            raise ValueError("上一轮预览不是受支持的 JPEG 分析预览")
        image.verify()
    return data


def load_previous_state(report_path, source_path):
    """绑定报告、分析、原工程和旧结果，返回核对后的上下文及预览字节。"""
    report_file = Path(report_path).expanduser().resolve()
    report_bytes = report_file.read_bytes()
    report = json.loads(report_bytes)
    if not isinstance(report, dict) or not isinstance(report.get("source"), dict):
        raise ValueError("上一轮执行记录缺少有效的 source")
    if (report.get("status") != "project_created_pending_visual_review"
        or report.get("project_photo_verified") is not True
        or report.get("photo_source_verified") is not True
        or report.get("not_applied_adjustments") != {}):
        raise ValueError("反馈入口需要已成功生成且四项基础建议均执行的记录")

    recorded = report["source"]
    source = Path(source_path).expanduser().resolve()
    if source != required_path(recorded, "project_path"):
        raise ValueError("请使用上一轮记录绑定的原始工程，不能把旧调色结果作为输入")
    analysis_file = required_path(recorded, "analysis_path")
    result, verified = load_verified_analysis(analysis_file, source)
    analysis_bytes = analysis_file.read_bytes()
    if hashlib.sha256(analysis_bytes).hexdigest() != verified["analysis_sha256"]:
        raise ValueError("上一轮分析读取期间发生变化")
    for key in ("analysis_sha256", "image_sha256", "preview_sha256", "image_layer_id", "image_file"):
        if recorded.get(key) != verified[key]:
            raise ValueError(f"上一轮报告与分析来源不一致：{key}")
    if recorded.get("reference") != verified.get("reference"):
        raise ValueError("上一轮报告与参考作品来源不一致")
    applied = report.get("applied_adjustments")
    if not isinstance(applied, dict) or set(applied) != set(PARAMETER_RANGES):
        raise ValueError("上一轮执行记录缺少完整的四项参数")
    checked = validate_analysis({"analysis": result["analysis"], "adjustment_plan": applied})
    if checked["adjustment_plan"] != result["adjustment_plan"]:
        raise ValueError("上一轮实际执行参数与分析方案不一致")

    # 不把已经被手动改过主要工具参数的工程当作原报告所描述的结果。
    old_project = required_path(report, "output_project")
    _, old_image_layer, old_image = get_project_image(old_project)
    if (old_image_layer["id"] != verified["image_layer_id"]
        or old_image_layer["imageFile"] != verified["image_file"]
        or hashlib.sha256(old_image.read_bytes()).hexdigest() != verified["image_sha256"]):
        raise ValueError("上一轮结果工程图片与分析来源不一致")
    manifest = json.loads((old_project / "manifest.json").read_text(encoding="utf-8"))
    plan = result["adjustment_plan"]
    expected = [
        (TARGET_LAYER_NAME, "Color Balance", "colorBalanceSettings", map_warmth_to_color_balance(plan["warmth_adjustment"])),
        (EXPOSURE_LAYER_NAME, "Exposure", "exposureSettings", map_exposure_ev(plan["exposure_ev"])),
        (CONTRAST_LAYER_NAME, "Curves", "curves", map_contrast_to_curves(plan["contrast_adjustment"])),
        (SATURATION_LAYER_NAME, "Hue/Saturation", "saturation", map_saturation_adjustment(plan["saturation_adjustment"])),
    ]
    for name, kind, key, settings in expected:
        matches = [layer for layer in manifest["layers"] if layer.get("name") == name]
        if len(matches) != 1:
            raise ValueError(f"上一轮结果缺少唯一的 {name}")
        layer = matches[0]
        adjustment = layer.get("adjustment", {})
        if (not isinstance(adjustment, dict)
            or layer.get("isVisible") is not True or layer.get("opacity", 1) != 1
            or layer.get("blendMode", "Normal") != "Normal" or layer.get("maskFile")
            or adjustment.get("kind") != kind or adjustment.get(key) != settings):
            raise ValueError(f"上一轮工具设置发生变化：{name}，请先核对原执行记录")
    tool_names = {item[0] for item in expected}
    if [p["name"] for p in manifest["layers"] if p.get("name") in tool_names] != [p[0] for p in expected]:
        raise ValueError("上一轮工具图层顺序发生变化")

    metadata = json.loads(analysis_bytes)["metadata"]
    if any(key not in metadata for key in (
        "image_path", "original_dimensions", "preview_dimensions", "preview_processing", "project_source",
    )):
        raise ValueError("上一轮分析缺少完整的照片预处理元数据")
    original_preview = read_verified_preview(required_path(metadata, "preview_path"), verified["preview_sha256"])
    reference_preview = None
    if "reference" in verified:
        reference = metadata["reference"]
        reference_preview = read_verified_preview(required_path(reference, "preview_path"), reference["preview_sha256"])
    instruction = metadata.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("上一轮分析缺少原始修图要求")
    state = {
        "previous_report_path": str(report_file),
        "previous_report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "previous_analysis_path": str(analysis_file),
        "previous_analysis_sha256": verified["analysis_sha256"],
        "previous_output_project": str(old_project),
        "previous_adjustment_plan": plan,
    }
    return result, metadata, state, original_preview, reference_preview


def run_refinement(previous_report, source_path, feedback, output_dir):
    """校验上一轮、请求新方案、保存，再复用现有工具从原工程执行。"""
    stage = "检查反馈输入"
    created = False
    saved = False
    run_dir = Path(output_dir).expanduser()
    try:
        print("[1/4] 核对上一轮记录、原工程和预览……", flush=True)
        if not isinstance(feedback, str) or not feedback.strip():
            raise ValueError("反馈不能为空")
        feedback = feedback.strip()
        if run_dir.exists() or run_dir.is_symlink():
            raise ValueError("结果目录已经存在，请换一个新名称")
        run_dir = run_dir.resolve()
        previous, metadata, state, original_bytes, reference_bytes = load_previous_state(previous_report, source_path)
        source = Path(source_path).expanduser().resolve()
        for project in (source, Path(state["previous_output_project"])):
            if run_dir == project or project in run_dir.parents:
                raise ValueError("新结果目录不能放在原工程或上一轮结果工程内部")
        api_key = load_api_key()
        run_dir.mkdir(parents=True, exist_ok=False)
        created = True
        preview_path = run_dir / "analysis-preview.jpg"
        with preview_path.open("xb") as file:
            file.write(original_bytes)
        reference_url = None
        reference = None
        if reference_bytes is not None:
            reference_path = run_dir / "reference-preview.jpg"
            with reference_path.open("xb") as file:
                file.write(reference_bytes)
            reference = {**metadata["reference"], "preview_path": str(reference_path)}
            reference_url = "data:image/jpeg;base64," + base64.b64encode(reference_bytes).decode("ascii")
        image_url = "data:image/jpeg;base64," + base64.b64encode(original_bytes).decode("ascii")

        stage = "请求反馈方案"
        print(f"[2/4] 请求 {MODEL} 根据文字反馈修订方案（自动重试关闭）……", flush=True)
        content = request_analysis(
            image_url, feedback, api_key, reference_url=reference_url,
            feedback_context={"original_instruction": metadata["instruction"], "previous_result": previous},
        )
        stage = "校验并保存反馈方案"
        print("[3/4] 校验四项最终参数与修改理由……", flush=True)
        result = validate_analysis(json.loads(content), require_reference=reference is not None, require_feedback=True)
        new_metadata = {
            **{key: metadata[key] for key in (
                "image_path", "image_sha256", "original_dimensions", "preview_sha256",
                "preview_dimensions", "preview_processing", "project_source",
            )},
            "instruction": metadata["instruction"], "model": MODEL,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "analysis_mode": "reference" if reference is not None else "automatic",
            "workflow": "feedback", "preview_path": str(preview_path),
            "feedback": {"text": feedback, **state},
        }
        if reference is not None:
            new_metadata["reference"] = reference
        analysis_path = run_dir / "analysis.json"
        save_result(analysis_path, result, new_metadata)
        saved = True

        stage = "从原工程执行反馈方案"
        print("[4/4] 再次核对来源，从原工程生成新版本……", flush=True)
        _, _, current_state, _, _ = load_previous_state(previous_report, source)
        if current_state != state:
            raise ValueError("上一轮记录在模型请求期间发生变化，已停止执行")
        project, report = apply_saved_analysis(analysis_path, source, run_dir / "result.comp")
        return analysis_path, project, report, result
    except (ValueError, OSError, APIConnectionError, APIStatusError) as error:
        detail = describe_error(error)
        if created:
            try:
                run_dir.rmdir()
            except OSError:
                pass
        if saved:
            detail += f"；新分析已保留在 {run_dir / 'analysis.json'}，请核对失败原因后再决定是否重试执行"
        elif created:
            detail += f"；预览或部分结果保留在 {run_dir}"
        raise PipelineError(f"{stage}失败：{detail}") from error


def main():
    parser = argparse.ArgumentParser(description="根据上一轮执行记录和文字反馈生成新的调色版本")
    parser.add_argument("--previous-report", required=True, help="上次四项基础执行的报告 JSON")
    parser.add_argument("--input", required=True, help="上轮记录绑定的原始 .comp 工程")
    parser.add_argument("--feedback", required=True, help="对上轮效果的文字反馈")
    parser.add_argument("--output", required=True, help="新的结果目录，必须尚不存在")
    args = parser.parse_args()
    try:
        analysis, project, report, result = run_refinement(
            args.previous_report, args.input, args.feedback, args.output,
        )
    except PipelineError as error:
        parser.exit(1, f"反馈调整失败：{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"分析：{analysis}\n工程：{project}\n执行记录：{report}")
    print("已从原工程应用四项最终参数；模型未看到调整后成片，请在 Compositor 中比较两个版本。")


if __name__ == "__main__":
    main()
