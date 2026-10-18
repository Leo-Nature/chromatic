"""统一入口：检查输入、分析照片、保存建议，并执行冷暖、饱和度、曝光和对比度。"""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from openai import APIConnectionError, APIStatusError, APITimeoutError

from apply_analysis import apply_saved_analysis
from compositor_tool import get_project_image
from main import MODEL, encode_photo, load_api_key, request_analysis, save_result, validate_analysis
from photo_project import verify_photo_import


class PipelineError(RuntimeError):
    """携带失败阶段的流程错误，供终端入口显示。"""


def describe_error(error):
    """生成易读错误信息，不展示远端原始响应体或凭证。"""
    if isinstance(error, APITimeoutError):
        return "模型请求超时，本次未自动重试"
    if isinstance(error, APIConnectionError):
        return "无法连接 DeepSeek，请检查网络"
    if isinstance(error, APIStatusError):
        hints = {401: "请检查 DeepSeek 密钥", 402: "请检查账户余额", 429: "请求受限，请稍后再试"}
        return f"服务返回 HTTP {error.status_code}，{hints.get(error.status_code, '请检查服务或请求配置')}"
    if isinstance(error, (json.JSONDecodeError, UnicodeError)):
        return "读取到的配置或模型正文不是有效 JSON 文本"
    return str(error)


def snapshot_inputs(source, image_layer, photo_source, reference_source):
    """描述已准备的输入，供界面比较预览和实际请求是否仍是同一份内容。"""
    return {
        "project_path": str(source),
        "manifest_sha256": hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest(),
        "image_layer_id": image_layer["id"], "image_file": image_layer["imageFile"],
        "image_sha256": photo_source["image_sha256"],
        "preview_sha256": photo_source["preview_sha256"],
        "reference_image_sha256": reference_source["image_sha256"] if reference_source else None,
        "reference_preview_sha256": reference_source["preview_sha256"] if reference_source else None,
    }


def run_pipeline(
    instruction, source_path, output_dir, reference_path=None,
    progress_callback=None, expected_inputs=None,
    expected_import=None,
):
    """复用各模块，返回分析、工程、报告的路径和本次建议。"""
    stage = "检查输入"
    run_created = False
    analysis_saved = False
    run_dir = Path(output_dir).expanduser()

    def notify(number, message):
        print(f"[{number}/4] {message}……", flush=True)
        if progress_callback is not None:
            progress_callback(number, message)

    try:
        notify(1, "检查输入")
        instruction = instruction.strip()
        if not instruction:
            raise ValueError("修图要求不能为空")
        if run_dir.exists() or run_dir.is_symlink():
            raise ValueError("结果目录已经存在，请换一个新的目录名称")
        run_dir = run_dir.resolve()
        source, image_layer, image_path = get_project_image(source_path)
        if run_dir == source or source in run_dir.parents:
            raise ValueError("结果目录不能放在原始工程内部")
        image_url, photo_source = encode_photo(image_path)
        reference_url = None
        reference_source = None
        if reference_path is not None:
            reference_url, reference_source = encode_photo(reference_path)
        if expected_inputs is not None:
            current_inputs = snapshot_inputs(source, image_layer, photo_source, reference_source)
            if current_inputs != expected_inputs:
                raise ValueError("工程、照片或参考作品已发生变化，请重新检查输入并确认预览")
        if expected_import is not None:
            verify_photo_import(expected_import, source, image_layer, photo_source)
        api_key = load_api_key()

        # 在请求前创建专属目录，确认目标可创建；exist_ok=False 拒绝抢占已有目录。
        run_dir.mkdir(parents=True, exist_ok=False)
        run_created = True
        analysis_path = run_dir / "analysis.json"
        project_path = run_dir / "result.comp"
        preview_path = run_dir / "analysis-preview.jpg"
        preview_bytes = base64.b64decode(image_url.split(",", 1)[1], validate=True)
        with preview_path.open("xb") as file:
            file.write(preview_bytes)
        if reference_source is not None:
            reference_preview = run_dir / "reference-preview.jpg"
            reference_bytes = base64.b64decode(reference_url.split(",", 1)[1], validate=True)
            with reference_preview.open("xb") as file:
                file.write(reference_bytes)
            reference_source["preview_path"] = str(reference_preview)
            print(f"参考作品预览：{reference_preview}", flush=True)
        print(f"分析来源：图层 {image_layer.get('name')}；预览：{preview_path}", flush=True)

        stage = "请求照片分析"
        notify(2, f"请求 {MODEL} 分析照片（自动重试关闭）")
        if reference_url is None:
            content = request_analysis(image_url, instruction, api_key)
        else:
            content = request_analysis(image_url, instruction, api_key, reference_url=reference_url)

        stage = "校验并保存分析"
        notify(3, "校验并保存分析")
        result = validate_analysis(json.loads(content), require_reference=reference_source is not None)
        metadata = {
            **photo_source,
            "instruction": instruction,
            "model": MODEL,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "analysis_mode": "reference" if reference_source is not None else "automatic",
            "preview_path": str(preview_path),
            "project_source": {
                "project_path": str(source),
                "image_layer_id": image_layer["id"],
                "image_file": image_layer["imageFile"],
            },
        }
        if reference_source is not None:
            metadata["reference"] = reference_source
        if expected_inputs is not None:
            metadata["confirmed_inputs"] = expected_inputs
        if expected_import is not None:
            metadata["photo_import"] = expected_import
        save_result(analysis_path, result, metadata)
        analysis_saved = True

        stage = "生成冷暖、饱和度、曝光和对比度调色工程"
        notify(4, "核对照片来源并生成四项调色工程")
        if expected_inputs is not None:
            current_manifest_hash = hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest()
            if current_manifest_hash != expected_inputs["manifest_sha256"]:
                raise ValueError("原工程配置在分析期间发生变化，已停止执行；请重新检查输入")
        if expected_import is not None:
            verify_photo_import(expected_import, source, image_layer, photo_source)
        project_path, report_path = apply_saved_analysis(
            analysis_path, source, project_path
        )
        return analysis_path, project_path, report_path, result
    except (ValueError, OSError, APIConnectionError, APIStatusError) as error:
        detail = describe_error(error)
        if run_created:
            # 只移除本次创建且仍为空的目录；任何已有结果都保留。
            try:
                run_dir.rmdir()
            except OSError:
                pass
        if analysis_saved:
            detail += f"；分析已保留在 {run_dir / 'analysis.json'}，可用 apply_analysis.py 重试执行"
        elif run_created and run_dir.exists():
            detail += f"；目录 {run_dir} 可能包含部分结果，请检查"
        raise PipelineError(f"{stage}失败：{detail}") from error


def main():
    parser = argparse.ArgumentParser(description="一次完成照片分析与 Compositor 冷暖、饱和度、曝光、对比度调色")
    parser.add_argument("--instruction", required=True, help="本次修图要求")
    parser.add_argument("--input", required=True, help="原始 Compositor 工程目录")
    parser.add_argument("--reference", help="可选参考作品 JPEG/PNG，仅借鉴风格")
    parser.add_argument("--output", required=True, help="本次运行的新结果目录，必须尚不存在")
    args = parser.parse_args()
    try:
        analysis, project, report, result = run_pipeline(
            args.instruction, args.input, args.output, reference_path=args.reference
        )
    except PipelineError as error:
        parser.exit(1, f"流程失败：{error}\n")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"分析：{analysis}\n工程：{project}\n执行记录：{report}")
    print("流程已完成，已执行冷暖、饱和度、曝光和对比度。")
    print(f"分析预览：{analysis.parent / 'analysis-preview.jpg'}")
    if args.reference is not None:
        print(f"参考预览：{analysis.parent / 'reference-preview.jpg'}")
        print("参考模式目前初步应用冷暖、饱和度、曝光和对比度，不代表完整风格迁移。")
    print("工程图片来源已核对，请对照预览并在 Compositor 中检查效果。")


if __name__ == "__main__":
    main()
