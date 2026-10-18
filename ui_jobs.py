"""页面任务准备与单次提交；复用现有预处理和调色流程。"""

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo

from compositor_tool import get_project_image
from main import encode_photo
from photo_project import create_photo_project, verify_photo_import
from refine_agent import (
    load_previous_state, read_verified_preview, required_path, run_refinement, snapshot_previous,
)
from run_agent import run_pipeline, snapshot_inputs


MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_PHOTO_BYTES = 50 * 1024 * 1024


def input_signature(source_path, instruction, reference_bytes=None):
    """识别输入框内容；改路径、要求或上传内容后必须重新准备。"""
    source = Path(source_path).expanduser()
    return {
        "source_path": str(source.resolve()), "instruction": instruction.strip(),
        "reference_sha256": hashlib.sha256(reference_bytes).hexdigest() if reference_bytes is not None else None,
    }


def photo_input_signature(photo_bytes, instruction, reference_bytes=None):
    """照片模式按原文件内容识别输入，不依赖用户上传的文件名。"""
    return {
        "input_mode": "photo", "instruction": instruction.strip(),
        "photo_sha256": hashlib.sha256(photo_bytes).hexdigest() if photo_bytes is not None else None,
        "reference_sha256": hashlib.sha256(reference_bytes).hexdigest() if reference_bytes is not None else None,
    }


def validate_job_text(instruction, reference_bytes):
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("请填写修图要求")
    if reference_bytes is not None and (not reference_bytes or len(reference_bytes) > MAX_REFERENCE_BYTES):
        raise ValueError("参考作品必须是非空文件，大小不超过 20 MiB")


def prepare_job(source_path, instruction, output_root, reference_bytes=None):
    """只检查和保存输入快照，不读取密钥、不调用模型。"""
    validate_job_text(instruction, reference_bytes)
    if not str(source_path).strip():
        raise ValueError("请填写原工程路径")
    source, image_layer, image_path = get_project_image(source_path)
    if source.suffix != ".comp":
        raise ValueError("原工程路径必须以 .comp 结尾")
    root = Path(output_root).expanduser().resolve()
    if root == source or source in root.parents:
        raise ValueError("界面结果目录不能位于原工程内部")
    image_url, photo_source = encode_photo(image_path)
    job_id = uuid.uuid4().hex
    job_dir = root / "ui-inputs" / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    return save_prepared_job(
        source, image_layer, image_url, photo_source, instruction, root, job_dir,
        reference_bytes, input_signature(source_path, instruction, reference_bytes),
    )


def prepare_photo_job(photo_bytes, instruction, output_root, reference_bytes=None):
    """本地保存上传副本、建立全尺寸工程，再走同一套预览准备逻辑。"""
    validate_job_text(instruction, reference_bytes)
    if not isinstance(photo_bytes, bytes) or not photo_bytes or len(photo_bytes) > MAX_PHOTO_BYTES:
        raise ValueError("请上传非空 JPEG/PNG 原照片，文件大小不超过 50 MiB")
    root = Path(output_root).expanduser().resolve()
    job_dir = root / "ui-inputs" / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=False)
    photo_path = job_dir / "photo-input"
    with photo_path.open("xb") as file:
        file.write(photo_bytes)
    source, report_path = create_photo_project(photo_path, job_dir / "source.comp")
    source, image_layer, image_path = get_project_image(source)
    image_url, photo_source = encode_photo(image_path)
    imported = {
        "photo_path": str(photo_path), "photo_sha256": hashlib.sha256(photo_bytes).hexdigest(),
        "report_path": str(report_path),
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
    }
    verify_photo_import(imported, source, image_layer, photo_source)
    return save_prepared_job(
        source, image_layer, image_url, photo_source, instruction, root, job_dir,
        reference_bytes, photo_input_signature(photo_bytes, instruction, reference_bytes), imported,
    )


def save_prepared_job(
    source, image_layer, image_url, photo_source, instruction, root, job_dir,
    reference_bytes, signature, imported=None,
):
    """两种入口共用参考检查、预览保存、来源快照及结果路径生成。"""
    reference_path = None
    reference_source = None
    reference_url = None
    if reference_bytes is not None:
        # 固定内部文件名，格式由已有预处理读取文件内容判定，不使用上传文件名拼路径。
        reference_path = job_dir / "reference-input"
        with reference_path.open("xb") as file:
            file.write(reference_bytes)
        reference_url, reference_source = encode_photo(reference_path)
    preview_path = job_dir / "analysis-preview.jpg"
    with preview_path.open("xb") as file:
        file.write(base64.b64decode(image_url.split(",", 1)[1], validate=True))
    reference_preview_path = None
    if reference_url is not None:
        reference_preview_path = job_dir / "reference-preview.jpg"
        with reference_preview_path.open("xb") as file:
            file.write(base64.b64decode(reference_url.split(",", 1)[1], validate=True))
    date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    job = {
        "job_dir": str(job_dir), "source_path": str(source), "instruction": instruction.strip(),
        "reference_path": str(reference_path) if reference_path is not None else None,
        "preview_path": str(preview_path),
        "reference_preview_path": str(reference_preview_path) if reference_preview_path is not None else None,
        "result_dir": str(root / f"ui-run-{date}-{job_dir.name[:8]}"),
        "input_signature": signature,
        "expected_inputs": snapshot_inputs(source, image_layer, photo_source, reference_source),
    }
    if imported is not None:
        job["expected_import"] = imported
    with (job_dir / "prepared.json").open("x", encoding="utf-8") as file:
        json.dump(job, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write("\n")
    return job


def job_submitted(job):
    """磁盘上的提交标记跨页面重运行保留，不代表请求或工程一定成功。"""
    return (Path(job["job_dir"]) / "submitted.json").exists()


def prepared_previews(job):
    expected = job["expected_inputs"]
    previews = {"original": read_verified_preview(Path(job["preview_path"]), expected["preview_sha256"])}
    if job["reference_preview_path"] is not None:
        previews["reference"] = read_verified_preview(
            Path(job["reference_preview_path"]), expected["reference_preview_sha256"],
        )
    return previews


def submit_job(job, current_signature, progress_callback=None):
    """每份输入快照只尝试一次；实际执行前由核心流程再次比较内容指纹。"""
    if current_signature != job["input_signature"]:
        raise ValueError("输入已修改，请重新检查并确认预览")
    prepared_previews(job)
    claim = Path(job["job_dir"]) / "submitted.json"
    try:
        with claim.open("x", encoding="utf-8") as file:
            json.dump({
                "attempted_at": datetime.now(timezone.utc).isoformat(),
                "output_dir": job["result_dir"],
            }, file, ensure_ascii=False)
    except FileExistsError as error:
        raise ValueError("这份输入已经提交过，请查看结果；再次运行需重新检查输入") from error
    # x 模式标记先于网络调用。失败或页面中断后也不自动重试，避免重复消耗 API。
    return run_pipeline(
        job["instruction"], job["source_path"], job["result_dir"],
        reference_path=job["reference_path"], progress_callback=progress_callback,
        expected_inputs=job["expected_inputs"],
        expected_import=job.get("expected_import"),
    )


def feedback_signature(report_path, feedback):
    """绑定当前所选报告和反馈文字；切换版本或改字后重新检查。"""
    return {"report_path": str(Path(report_path).expanduser().resolve()), "feedback": feedback.strip()}


def prepare_feedback_job(report_path, feedback, output_root):
    """从报告找回原工程并核对完整上下文，不请求模型。"""
    if not isinstance(feedback, str) or not feedback.strip():
        raise ValueError("请填写对当前版本的反馈")
    report_file = Path(report_path).expanduser().resolve()
    report = json.loads(report_file.read_bytes())
    if not isinstance(report, dict) or not isinstance(report.get("source"), dict):
        raise ValueError("当前报告缺少照片来源")
    source = required_path(report["source"], "project_path")
    _, _, state, _, _ = load_previous_state(report_file, source)
    root = Path(output_root).expanduser().resolve()
    for project in (source, Path(state["previous_output_project"])):
        if root == project or project in root.parents:
            raise ValueError("界面结果目录不能位于原工程或上一轮结果工程内部")
    job_id = uuid.uuid4().hex
    job_dir = root / "ui-feedback-inputs" / job_id
    date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    job = {
        "job_dir": str(job_dir), "source_path": str(source),
        "previous_report": str(report_file), "feedback": feedback.strip(),
        "input_signature": feedback_signature(report_file, feedback),
        "expected_previous": snapshot_previous(source, state),
        "result_dir": str(root / f"ui-feedback-{date}-{job_id[:8]}"),
    }
    job_dir.mkdir(parents=True, exist_ok=False)
    with (job_dir / "prepared.json").open("x", encoding="utf-8") as file:
        json.dump(job, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write("\n")
    return job


def submit_feedback_job(job, current_signature, progress_callback=None):
    """一次确认只尝试一次，核心入口在请求前和执行前再次核对版本。"""
    if current_signature != job["input_signature"]:
        raise ValueError("版本或反馈已修改，请重新检查反馈")
    try:
        with (Path(job["job_dir"]) / "submitted.json").open("x", encoding="utf-8") as file:
            json.dump({
                "attempted_at": datetime.now(timezone.utc).isoformat(),
                "output_dir": job["result_dir"],
            }, file, ensure_ascii=False)
    except FileExistsError as error:
        raise ValueError("这份反馈已经提交过，再次运行需重新检查反馈") from error
    return run_refinement(
        job["previous_report"], job["source_path"], job["feedback"], job["result_dir"],
        progress_callback=progress_callback, expected_previous=job["expected_previous"],
    )
