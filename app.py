"""Chromatic 本地界面：查看记录，明确提交初次调色和版本反馈。"""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

import streamlit as st

from apply_analysis import load_verified_analysis
from compositor_tool import get_project_image
from refine_agent import read_verified_preview, required_path
from run_agent import PipelineError
from ui_jobs import (
    feedback_signature, input_signature, job_submitted, prepare_feedback_job,
    photo_input_signature, prepare_job, prepare_photo_job, prepared_previews,
    submit_feedback_job, submit_job,
)


PROJECT_DIR = Path(__file__).resolve().parent
COMPOSITOR_APP = Path("/Applications/Compositor.app")
PARAMETER_LABELS = {
    "warmth_adjustment": "冷暖", "exposure_ev": "曝光",
    "contrast_adjustment": "对比度", "saturation_adjustment": "饱和度",
}
ANALYSIS_LABELS = {
    "exposure": "曝光", "white_balance": "白平衡",
    "contrast": "对比度", "color_tendency": "色彩倾向",
}
REFERENCE_LABELS = {
    "warmth": "冷暖氛围", "contrast": "明暗反差",
    "saturation": "色彩浓度", "tone": "整体影调",
}


def read_report(path):
    with Path(path).open(encoding="utf-8") as file:
        report = json.load(file)
    if not isinstance(report, dict):
        raise ValueError("执行记录应为 JSON 对象")
    return report


def discover_reports(output_dir):
    """只查 outputs 根目录及下一层，不递归读取大型 .comp 图片目录。"""
    root = Path(output_dir).resolve()
    paths = set(root.glob("*.report.json")) | set(root.glob("*/*.report.json"))

    def created_at(path):
        try:
            value = read_report(path).get("created_at", "")
            return value if isinstance(value, str) else ""
        except (ValueError, OSError):
            return ""

    return sorted((p for p in paths if p.is_file() and p.resolve().is_relative_to(root)),
                  key=lambda p: (created_at(p), str(p)), reverse=True)


def report_label(path, output_dir):
    """日期和相对位置共同标记版本，避免重名 result.report.json 混淆。"""
    relative = str(Path(path).relative_to(Path(output_dir)))
    try:
        report = read_report(path)
        date = datetime.fromisoformat(report.get("created_at", "")).astimezone(ZoneInfo("Asia/Shanghai"))
        kind = "反馈调整" if "feedback" in report else "初次调色"
        return f"{date:%m-%d %H:%M} · {kind} · {relative}"
    except (ValueError, OSError, TypeError):
        return f"待检查记录 · {relative}"


def check_output_project(project, verified_source):
    """确认按钮指向一个包含同一照片的 Compositor 工程。"""
    if project.suffix != ".comp":
        raise ValueError("结果路径不是 .comp 工程")
    _, layer, image = get_project_image(project)
    if (layer["id"] != verified_source["image_layer_id"]
        or layer["imageFile"] != verified_source["image_file"]
        or hashlib.sha256(image.read_bytes()).hexdigest() != verified_source["image_sha256"]):
        raise ValueError("结果工程的照片与这份记录不一致，不能打开")


def load_result_view(report_path):
    """核对记录与分析来源；预览或结果缺失时仍允许阅读有效分析。"""
    report = read_report(report_path)
    source = report.get("source")
    if not isinstance(source, dict) or report.get("project_photo_verified") is not True:
        raise ValueError("此记录没有有效的工程照片来源绑定，请选择新版结果")
    analysis_path = required_path(source, "analysis_path")
    result, verified = load_verified_analysis(analysis_path, required_path(source, "project_path"))
    analysis_bytes = analysis_path.read_bytes()
    if hashlib.sha256(analysis_bytes).hexdigest() != verified["analysis_sha256"]:
        raise ValueError("分析文件在读取时发生变化，请刷新记录")
    for key in ("analysis_sha256", "image_sha256", "preview_sha256", "image_layer_id", "image_file"):
        if source.get(key) != verified[key]:
            raise ValueError(f"记录与分析来源不一致：{key}")
    if source.get("reference") != verified.get("reference"):
        raise ValueError("记录与分析中的参考来源不一致")
    applied = report.get("applied_adjustments")
    plan = result["adjustment_plan"]
    if not isinstance(applied, dict) or not applied:
        raise ValueError("记录缺少已执行参数")
    for key, value in applied.items():
        if (key not in plan or isinstance(value, bool) or not isinstance(value, (int, float))
            or value != plan[key]):
            raise ValueError("记录中的执行参数与分析方案不一致")
    if report.get("not_applied_adjustments") != {k: v for k, v in plan.items() if k not in applied}:
        raise ValueError("记录中的未执行项目与方案不一致")
    if verified.get("feedback") != report.get("feedback"):
        raise ValueError("反馈记录与分析来源不一致")

    metadata = json.loads(analysis_bytes)["metadata"]
    previews = {}
    preview_errors = {}
    sources = {"original": metadata}
    if "reference" in verified:
        sources["reference"] = metadata["reference"]
    for name, info in sources.items():
        try:
            previews[name] = read_verified_preview(required_path(info, "preview_path"), info["preview_sha256"])
        except (ValueError, OSError) as error:
            preview_errors[name] = str(error)
    project = required_path(report, "output_project")
    project_error = None
    try:
        check_output_project(project, verified)
    except (ValueError, OSError) as error:
        project_error = str(error)
    return {
        "report": report, "result": result, "metadata": metadata, "source": verified,
        "previews": previews, "preview_errors": preview_errors,
        "project": project, "project_error": project_error,
    }


def open_result_project(report_path):
    """仅在点击后重新核对所选工程，再交给本机 Compositor；不使用 shell。"""
    view = load_result_view(report_path)
    if view["project_error"]:
        raise ValueError(view["project_error"])
    if sys.platform != "darwin" or not COMPOSITOR_APP.is_dir():
        raise ValueError("此按钮需要在安装了 Compositor 的本机 Mac 上使用")
    subprocess.run(
        ["/usr/bin/open", "-a", str(COMPOSITOR_APP), str(view["project"])],
        check=True, capture_output=True, text=True, timeout=15,
    )


def render_new_job(root):
    """准备和提交分开；普通控件重运行只读状态，不发模型请求。"""
    with st.expander("新建调色", expanded=False):
        st.caption("上传原照片或使用已有工程；先检查并确认预览，再提交分析。")
        input_mode = st.radio(
            "输入方式", ["上传原照片", "使用已有工程"], horizontal=True, key="new_input_mode",
        )
        left, right = st.columns([3, 2])
        source_text = None
        photo_bytes = None
        with left:
            if input_mode == "上传原照片":
                photo = st.file_uploader(
                    "原照片（JPEG / PNG）", type=["jpg", "jpeg", "png"],
                    max_upload_size=50, key="new_photo",
                )
                photo_bytes = photo.getvalue() if photo is not None else None
                st.caption("最多 50 MiB、8000 万像素。检查时保存原文件副本，并建立全尺寸原始工程。")
            else:
                source_text = st.text_input(
                    "原工程路径", value=str(PROJECT_DIR / "outputs/test-photo-source.comp"),
                    help="填写本机 .comp 原工程目录。照片仍从工程中读取。", key="new_source",
                )
            instruction = st.text_area(
                "修图要求", value="保持自然，稍微暖一点，保留主体细节，不要过度饱和。",
                key="new_instruction", height=100,
            )
        with right:
            upload = st.file_uploader(
                "参考作品（可选，JPEG / PNG）", type=["jpg", "jpeg", "png"],
                max_upload_size=20, key="new_reference",
            )
            st.caption("支持截图，单文件最多 20 MiB。留空则按原照片和要求分析。")
        reference_bytes = upload.getvalue() if upload is not None else None
        try:
            signature = (photo_input_signature(photo_bytes, instruction, reference_bytes)
                         if input_mode == "上传原照片" else
                         input_signature(source_text, instruction, reference_bytes))
        except (ValueError, OSError) as error:
            st.session_state.pop("prepared_job", None)
            st.error(f"输入不可用：{error}")
            return
        job = st.session_state.get("prepared_job")
        if job is not None and signature != job["input_signature"]:
            st.session_state.pop("prepared_job", None)
            st.session_state.pop("job_notice", None)
            job = None
            st.info("输入已修改，请重新检查并确认预览。")
        if st.button("检查输入并预览", key="prepare_job"):
            st.session_state.pop("prepared_job", None)
            st.session_state.pop("job_notice", None)
            job = None
            try:
                with st.spinner("正在检查图片、准备原始工程与预览……"):
                    if input_mode == "上传原照片":
                        job = prepare_photo_job(photo_bytes, instruction, root, reference_bytes)
                    else:
                        job = prepare_job(source_text, instruction, root, reference_bytes)
                st.session_state["prepared_job"] = job
            except (ValueError, OSError) as error:
                st.error(f"输入检查失败：{error}")
        if job is None:
            return
        try:
            previews = prepared_previews(job)
        except (ValueError, OSError) as error:
            st.error(f"待分析预览不可用，请重新检查输入：{error}")
            return
        st.markdown("**本次待分析输入**")
        if job.get("expected_import") is not None:
            st.caption("原照片副本与全尺寸原始工程已保存。下方预览从该工程读取，后续反馈也以该工程为起点。")
        original_column, reference_column = st.columns(2)
        with original_column:
            st.image(previews["original"], caption="待分析原照片预览", width=260)
        with reference_column:
            if "reference" in previews:
                st.image(previews["reference"], caption="待分析参考作品预览", width=260)
            else:
                st.caption("本次未使用参考作品。")
        st.caption("仅点击下面的按钮才会向 DeepSeek 发送预览并产生 API 用量。每次确认的输入只提交一次。")
        notice = st.session_state.get("job_notice")
        if notice:
            level, message = notice
            getattr(st, level)(message)
        submitted = job_submitted(job)
        if submitted:
            st.caption("这份输入已经提交过。再次运行请点击“检查输入并预览”，确认新的提交。")
        if st.button("开始分析并调色", type="primary", key="submit_job", disabled=submitted):
            status = st.status("正在提交任务……", expanded=True)

            def progress(number, message):
                status.update(label=f"{number}/4 · {message}")
                status.write(f"{number}. {message}")

            try:
                _, _, report, _ = submit_job(job, signature, progress_callback=progress)
            except (ValueError, OSError, PipelineError) as error:
                status.update(label="本次任务未完成", state="error", expanded=True)
                message = str(error)
                st.session_state["job_notice"] = ("error", message)
                st.error(message)
            else:
                status.update(label="调色工程已生成", state="complete", expanded=False)
                st.session_state["report_path"] = str(report)
                st.session_state["job_notice"] = ("success", "调色工程已生成，下方已选中新结果。")
                st.rerun()


def render_feedback_job(root, selected, view):
    """反馈绑定所选版本；成功后先记录待选路径，下次重运行再更新选择控件。"""
    with st.expander("针对当前版本提交反馈", expanded=False):
        st.caption(f"反馈对象：{Path(selected).relative_to(root)}")
        report = view["report"]
        unavailable = (
            set(report["applied_adjustments"]) != set(PARAMETER_LABELS)
            or report["not_applied_adjustments"] != {}
            or report.get("status") != "project_created_pending_visual_review"
            or report.get("photo_source_verified") is not True
            or view["project_error"] is not None or bool(view["preview_errors"])
        )
        if unavailable:
            st.session_state.pop("prepared_feedback", None)
            st.info("当前版本暂不能提交反馈：需要完整四项执行记录、可用预览及结果工程。")
            return
        feedback = st.text_area(
            "对当前版本的反馈", key="feedback_text", height=100,
            placeholder="例如：将冷暖最终值调整为 0.05，其他参数保持不变。",
        )
        signature = feedback_signature(selected, feedback)
        job = st.session_state.get("prepared_feedback")
        if job is not None and signature != job["input_signature"]:
            st.session_state.pop("prepared_feedback", None)
            st.session_state.pop("feedback_error", None)
            job = None
            st.info("版本或反馈已修改，请重新检查反馈。")
        st.caption("复用该版本绑定的原工程和参考预览。新方案从原工程执行，旧版本保留。模型未看到调色后的成片。")
        if st.button("检查反馈", key="prepare_feedback"):
            st.session_state.pop("prepared_feedback", None)
            st.session_state.pop("feedback_error", None)
            job = None
            try:
                with st.spinner("正在核对版本、来源与预览……"):
                    job = prepare_feedback_job(selected, feedback, root)
                st.session_state["prepared_feedback"] = job
            except (ValueError, OSError) as error:
                st.error(f"反馈检查失败：{error}")
        if job is None:
            return
        st.markdown("**已核对的反馈对象与参数**")
        st.code(job["previous_report"], language=None)
        plan = job["expected_previous"]["state"]["previous_adjustment_plan"]
        st.table([{"参数": label, "当前值": plan[key]} for key, label in PARAMETER_LABELS.items()])
        st.write(f"待提交反馈：{job['feedback']}")
        st.caption("检查未调用模型。点击下方按钮才会请求 DeepSeek，并产生 API 用量。")
        error = st.session_state.get("feedback_error")
        if error:
            st.error(error)
        submitted = job_submitted(job)
        if submitted:
            st.caption("这份反馈已经提交过，不会自动重试。再次运行需重新点击“检查反馈”。")
        if st.button("根据反馈生成新版本", type="primary", key="submit_feedback", disabled=submitted):
            status = st.status("正在提交反馈……", expanded=True)

            def progress(number, message):
                status.update(label=f"{number}/4 · {message}")
                status.write(f"{number}. {message}")

            try:
                _, _, new_report, _ = submit_feedback_job(job, signature, progress_callback=progress)
            except (ValueError, OSError, PipelineError) as error:
                status.update(label="本次反馈未完成", state="error", expanded=True)
                st.session_state["feedback_error"] = str(error)
            else:
                status.update(label="反馈新版本已生成", state="complete", expanded=False)
                # 选择控件本轮已经创建，不能在这里直接改它绑定的 report_path。
                st.session_state["pending_report_path"] = str(new_report)
                st.session_state["feedback_success"] = "反馈新版本已生成，已自动选中新结果。"
            st.rerun()


def render_page(output_dir=None):
    """查看不调用模型；初次调色与反馈都需要单独检查和明确提交。"""
    root = Path(output_dir).resolve() if output_dir is not None else PROJECT_DIR / "outputs"
    st.set_page_config(page_title="Chromatic · 调色记录", page_icon="🎨", layout="wide")
    # 只定制排版；背景和文字交给 Streamlit 主题配对，避免 Dark 模式浅字叠浅底。
    st.markdown("""<style>
        .block-container { max-width: 1320px; padding-top: 2.4rem; }
        h1, h2, h3 { letter-spacing: -.025em; }
        [data-testid="stMetric"] { border-radius: 12px; padding: 1rem; }
    </style>""", unsafe_allow_html=True)
    st.title("Chromatic")
    st.caption("查看调色建议，比较每一轮选择。")
    if pending := st.session_state.pop("pending_report_path", None):
        st.session_state["report_path"] = pending
    if success := st.session_state.pop("feedback_success", None):
        st.success(success)
    render_new_job(root)

    with st.sidebar:
        st.subheader("调色记录")
        st.caption("选择想查看的版本，最新记录排在前面。")
        st.button("刷新记录", key="refresh_records", width="stretch")
        paths = discover_reports(root)
        if not paths:
            st.info("还没有执行记录。可以在“新建调色”中开始一次任务，或完成命令行调色后刷新。")
            return
        options = [str(path) for path in paths]
        if st.session_state.get("report_path") not in options:
            st.session_state["report_path"] = options[0]
        selected = st.selectbox(
            "选择版本", options, format_func=lambda p: report_label(p, root),
            key="report_path", width="stretch",
        )
        prepared = st.session_state.get("prepared_feedback")
        if prepared is not None and prepared["previous_report"] != selected:
            st.session_state.pop("prepared_feedback", None)
            st.session_state.pop("feedback_error", None)
        st.caption(f"共 {len(paths)} 份记录")
        st.divider()
        st.caption("查看和切换记录不会调用 AI。新任务需要预览确认和明确提交。")

    try:
        view = load_result_view(selected)
    except (ValueError, OSError) as error:
        st.error(f"暂时无法查看这份记录：{error}")
        st.caption("请确认原工程与分析文件仍在原位置，或切换到另一份记录。")
        return
    report, result = view["report"], view["result"]
    applied = report["applied_adjustments"]
    heading, action = st.columns([3, 2])
    with heading:
        st.subheader("反馈调整" if "feedback" in view["source"] else "初次调色")
        st.caption(str(Path(selected).relative_to(root)))
    with action:
        if st.button("在 Compositor 中打开", type="primary", key="open_project",
                     disabled=view["project_error"] is not None, width="stretch"):
            try:
                open_result_project(selected)
                st.success("已交给 Compositor 打开。")
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                st.error(f"打开失败：{error}")
    if view["project_error"]:
        st.warning(f"结果工程暂时无法打开：{view['project_error']}")

    original, reference, intent = st.columns([1, 1, 1.3])
    for column, name, title in ((original, "original", "原照片分析预览"),
                                (reference, "reference", "参考作品预览")):
        with column:
            st.markdown(f"**{title}**")
            if name in view["previews"]:
                st.image(view["previews"][name], width=260)
            elif name in view["preview_errors"]:
                st.warning(f"预览不可用：{view['preview_errors'][name]}")
            else:
                st.caption("本轮没有参考作品。")
    with intent:
        st.markdown("**原始修图要求**")
        st.write(view["metadata"].get("instruction", "未记录"))
        feedback = view["source"].get("feedback")
        if feedback:
            st.markdown("**本轮反馈**")
            st.write(feedback["text"])
        st.info("这里展示的是分析输入。调色后的画面请在 Compositor 中查看。")

    st.subheader("本次执行参数")
    columns = st.columns(4)
    for column, (key, label) in zip(columns, PARAMETER_LABELS.items()):
        value = f"{applied[key]:+.2f}" if key in applied else "未执行"
        if key == "exposure_ev" and key in applied:
            value += " EV"
        column.metric(label, value)
    st.caption("冷暖、对比度和饱和度为方案建议值，不是视觉百分比。参数来自保存的执行记录；工程中的手动修改不会自动更新此记录。")

    render_feedback_job(root, selected, view)

    analysis_tab, reference_tab, feedback_tab, files_tab = st.tabs(["分析建议", "参考风格", "反馈记录", "文件位置"])
    with analysis_tab:
        for key, label in ANALYSIS_LABELS.items():
            st.markdown(f"**{label}**")
            st.write(result["analysis"][key])
        if report["not_applied_adjustments"]:
            st.info("这是一份历史记录，部分建议当时尚未执行：" + "、".join(
                PARAMETER_LABELS[key] for key in report["not_applied_adjustments"]
            ))
    with reference_tab:
        if "reference_analysis" in result:
            for key, label in REFERENCE_LABELS.items():
                st.markdown(f"**{label}**")
                st.write(result["reference_analysis"][key])
            st.markdown("**借鉴方式**")
            st.write(result["transfer_explanation"])
        else:
            st.caption("本轮没有参考风格分析。")
    with feedback_tab:
        if feedback:
            st.write(result["feedback_explanation"])
            st.table([
                {"参数": label, "上一轮": feedback["previous_adjustment_plan"][key],
                 "本轮": applied[key], "变化": applied[key] - feedback["previous_adjustment_plan"][key]}
                for key, label in PARAMETER_LABELS.items() if key in applied
            ])
            st.caption("模型依据原照片、可选参考预览和文字反馈修订方案，未直接查看调整后成片。")
        else:
            st.caption("这是初次调色，没有上一轮反馈。")
    with files_tab:
        for label, path in (("结果工程", view["project"]), ("执行记录", selected),
                            ("分析文件", view["source"]["analysis_path"])):
            st.markdown(f"**{label}**")
            st.code(str(path), language=None)


if __name__ == "__main__":
    render_page()
