"""复制 Compositor 工程，并用固定参数调整指定图层的冷暖。"""

import argparse
import json
import math
from pathlib import Path
import shutil


TARGET_LAYER_NAME = "Test Color Balance"


def map_warmth_to_color_balance(warmth_adjustment):
    """把 [-1, 1] 的冷暖数值转换成色彩平衡设置；正值预期偏暖。"""
    if isinstance(warmth_adjustment, bool) or not isinstance(
        warmth_adjustment, (int, float)
    ):
        raise ValueError("冷暖参数必须是数字，不能是布尔值或文字")
    if not math.isfinite(warmth_adjustment) or not -1 <= warmth_adjustment <= 1:
        raise ValueError("冷暖参数必须是 -1 到 1 之间的有限数值")

    # 沿用原实验的幅度，视觉效果仍需在 Compositor 中验证。
    amount = round(warmth_adjustment * 20)
    return {
        "shadowCyanRed": amount,
        "shadowMagentaGreen": 0,
        "shadowYellowBlue": -amount,
        "midCyanRed": amount,
        "midMagentaGreen": 0,
        "midYellowBlue": -amount,
        "highlightCyanRed": amount,
        "highlightMagentaGreen": 0,
        "highlightYellowBlue": -amount,
        "preserveLuminosity": True,
    }


def create_adjusted_project(source_path, output_path, warmth_adjustment):
    """检查输入、复制完整工程、修改副本，返回输出工程的绝对路径。"""
    settings = map_warmth_to_color_balance(warmth_adjustment)
    source = Path(source_path).expanduser().resolve()
    requested_output = Path(output_path).expanduser()
    # 也拒绝失效的符号链接，避免意外写到链接指向的位置。
    if requested_output.exists() or requested_output.is_symlink():
        raise ValueError("输出路径已经存在，请换一个新的工程名称")
    output = requested_output.resolve()

    if not source.is_dir():
        raise ValueError(f"找不到源工程目录：{source}")
    if output == source or source in output.parents:
        raise ValueError("输出不能是原始工程，也不能放在原始工程内部")
    if output.suffix != ".comp":
        raise ValueError("输出工程名称必须以 .comp 结尾")

    manifest_path = source / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("源工程缺少 manifest.json")
    with manifest_path.open(encoding="utf-8") as file:
        manifest = json.load(file)
    if not isinstance(manifest, dict) or manifest.get("format") != "com.compositor.project":
        raise ValueError("manifest 不是受支持的 Compositor 工程配置")
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not all(isinstance(layer, dict) for layer in layers):
        raise ValueError("manifest 缺少有效的 layers 图层列表")

    matches = [layer for layer in layers if layer.get("name") == TARGET_LAYER_NAME]
    if len(matches) != 1:
        raise ValueError(f"工程必须恰好包含一个名为 {TARGET_LAYER_NAME} 的图层")
    target_layer = matches[0]
    adjustment = target_layer.get("adjustment")
    if not isinstance(adjustment, dict) or adjustment.get("kind") != "Color Balance":
        raise ValueError(f"{TARGET_LAYER_NAME} 必须是 Color Balance 调整图层")

    # 检查当前示例工程顶层图层引用的图片，避免生成缺图的副本。
    for layer in layers:
        if "imageFile" in layer:
            image_name = layer["imageFile"]
            if not isinstance(image_name, str) or not image_name:
                raise ValueError("图层的 imageFile 必须是非空文件名")
            image_path = (source / "images" / image_name).resolve()
            if source not in image_path.parents or not image_path.is_file():
                raise ValueError(f"图层图片缺失或位于工程外部：{image_name}")

    # 这里先修改内存中的字典；原始 manifest 文件没有被写入。
    adjustment["colorBalanceSettings"] = settings
    target_layer["isVisible"] = True

    # copytree 复制整个目录，包括图片和其他工程资源；默认拒绝覆盖。
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, output)
    with (output / "manifest.json").open("w", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, indent=2)
        file.write("\n")
    return output


def main():
    """把终端参数交给工具函数，并向用户报告执行结果。"""
    parser = argparse.ArgumentParser(description="复制 Compositor 工程并调整冷暖")
    parser.add_argument("--input", required=True, help="原始 .comp 工程目录")
    parser.add_argument("--output", required=True, help="新的 .comp 工程路径，必须尚不存在")
    parser.add_argument("--warmth", required=True, type=float, help="冷暖值，范围 -1 到 1")
    args = parser.parse_args()

    try:
        output = create_adjusted_project(args.input, args.output, args.warmth)
    except (ValueError, OSError) as error:
        parser.exit(1, f"生成失败：{error}\n")

    print(f"已生成工程：{output}")
    print(f"冷暖参数：{args.warmth}；已启用图层：{TARGET_LAYER_NAME}")
    print("请在 Compositor 中打开工程，开关调整图层检查效果。")


# 直接运行此文件时才启动命令行；其他模块导入函数时不会复制工程。
if __name__ == "__main__":
    main()
