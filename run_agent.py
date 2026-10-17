"""统一入口：检查输入、分析照片、保存建议，并执行冷暖调色。"""

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path

from openai import APIConnectionError, APIStatusError, APITimeoutError

from apply_analysis import apply_saved_analysis
from compositor_tool import get_project_image
from main import MODEL, encode_photo, load_api_key, request_analysis, save_result, validate_analysis


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


def run_pipeline(instruction, source_path, output_dir):
    """复用各模块，返回分析、工程、报告的路径和本次建议。"""
    stage = "检查输入"
    run_created = False
    analysis_saved = False
    run_dir = Path(output_dir).expanduser()
    try:
        print("[1/4] 检查输入……", flush=True)
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
        print(f"分析来源：图层 {image_layer.get('name')}；预览：{preview_path}", flush=True)

        stage = "请求照片分析"
        print(f"[2/4] 请求 {MODEL} 分析照片（自动重试关闭）……", flush=True)
        content = request_analysis(image_url, instruction, api_key)

        stage = "校验并保存分析"
        print("[3/4] 校验并保存分析……", flush=True)
        result = validate_analysis(json.loads(content))
        metadata = {
            **photo_source,
            "instruction": instruction,
            "model": MODEL,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "preview_path": str(preview_path),
            "project_source": {
                "project_path": str(source),
                "image_layer_id": image_layer["id"],
                "image_file": image_layer["imageFile"],
            },
        }
        save_result(analysis_path, result, metadata)
        analysis_saved = True

        stage = "生成冷暖调色工程"
        print("[4/4] 核对照片来源并生成冷暖调色工程……", flush=True)
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
    parser = argparse.ArgumentParser(description="一次完成照片分析与 Compositor 冷暖调色")
    parser.add_argument("--instruction", required=True, help="本次修图要求")
    parser.add_argument("--input", required=True, help="原始 Compositor 工程目录")
    parser.add_argument("--output", required=True, help="本次运行的新结果目录，必须尚不存在")
    args = parser.parse_args()
    try:
        analysis, project, report, result = run_pipeline(
            args.instruction, args.input, args.output
        )
    except PipelineError as error:
        parser.exit(1, f"流程失败：{error}\n")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"分析：{analysis}\n工程：{project}\n执行记录：{report}")
    print("流程已完成，已执行冷暖；曝光、对比度、饱和度未执行。")
    print(f"分析预览：{analysis.parent / 'analysis-preview.jpg'}")
    print("工程图片来源已核对，请对照预览并在 Compositor 中检查效果。")


if __name__ == "__main__":
    main()
