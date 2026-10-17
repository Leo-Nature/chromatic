"""复制 Compositor 工程，执行冷暖与可选的饱和度、曝光、对比度调整。"""

import argparse
import json
import math
from pathlib import Path
import shutil
import uuid


TARGET_LAYER_NAME = "Test Color Balance"
SATURATION_LAYER_NAME = "AI Saturation"
EXPOSURE_LAYER_NAME = "AI Exposure"
CONTRAST_LAYER_NAME = "AI Contrast"


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


def map_saturation_adjustment(value):
    """把建议范围 [-1, 1] 映射到 Compositor 的 [-100, 100] 参数范围。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("饱和度参数必须是数字，不能是布尔值或文字")
    if not -1 <= value <= 1 or not math.isfinite(value):
        raise ValueError("饱和度参数必须是 -1 到 1 之间的有限数值")
    # 这是范围换算，不代表已校准的视觉百分比。
    return round(value * 100)


def map_exposure_ev(value):
    """EV 直接对应曝光档数；其余曝光控件保持中性。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("曝光参数必须是数字，不能是布尔值或文字")
    if not -2 <= value <= 2 or not math.isfinite(value):
        raise ValueError("曝光参数必须是 -2 到 2 之间的有限 EV 数值")
    return {"exposure": float(value), "offset": 0, "gamma": 1}


def map_contrast_to_curves(value):
    """把 [-1, 1] 建议转换为 RGB 总曲线；幅度为暂定规则，非百分比。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("对比度参数必须是数字，不能是布尔值或文字")
    if not -1 <= value <= 1 or not math.isfinite(value):
        raise ValueError("对比度参数必须是 -1 到 1 之间的有限数值")
    amount = float(value) * 32
    master = [
        {"x": 0, "y": 0},
        {"x": 64, "y": 64 - amount},
        {"x": 128, "y": 128},
        {"x": 192, "y": 192 + amount},
        {"x": 255, "y": 255},
    ]
    # 四组依次为 RGB 总曲线、红、绿、蓝；单独颜色通道保持中性。
    return {"channel": "RGB", "channels": [master, *[
        [{"x": 0, "y": 0}, {"x": 255, "y": 255}] for _ in range(3)
    ]]}


def new_adjustment_layer(manifest, name, adjustment):
    """构造全画布调整层，共用中性的 Levels、Curves 等基础设置。"""
    dimensions = [manifest.get("width"), manifest.get("height")]
    if not all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in dimensions):
        raise ValueError("新增调整图层需要有效的画布宽高")
    layer_id = str(uuid.uuid4()).upper()
    while any(layer.get("id") == layer_id for layer in manifest["layers"]):
        layer_id = str(uuid.uuid4()).upper()
    return {
        "id": layer_id,
        "name": name,
        "isGroup": False,
        "isVisible": True,
        "opacity": 1,
        "blendMode": "Normal",
        "transform": {
            "origin": [0, 0], "size": dimensions, "rotation": 0,
            "flipX": False, "flipY": False, "sampling": "High quality",
        },
        "adjustment": {
            "hue": 0, "saturation": 0, "lightness": 0, "colorize": False,
            "levels": {"channel": "RGB", "ranges": [
                {"black": 0, "gamma": 1, "white": 255, "outputBlack": 0, "outputWhite": 255}
                for _ in range(4)
            ]},
            "curves": {"channel": "RGB", "channels": [
                [{"x": 0, "y": 0}, {"x": 255, "y": 255}] for _ in range(4)
            ]},
            **adjustment,
        },
    }


def set_exposure_layer(manifest, settings):
    """新增或复用曝光层，替换 EV 并将 offset、gamma 重置为中性。"""
    layers = manifest["layers"]
    matches = [layer for layer in layers if layer.get("name") == EXPOSURE_LAYER_NAME]
    if len(matches) > 1:
        raise ValueError("存在多个 AI Exposure 图层，无法确定调整对象")
    if matches:
        layer = matches[0]
        adjustment = layer.get("adjustment")
        if (
            layer.get("isGroup") or "imageFile" in layer
            or not isinstance(adjustment, dict) or adjustment.get("kind") != "Exposure"
        ):
            raise ValueError("AI Exposure 名称已被其他类型图层占用")
        adjustment["exposureSettings"] = settings
        layer["isVisible"] = True
        layers.remove(layer)
    else:
        layer = new_adjustment_layer(manifest, EXPOSURE_LAYER_NAME, {
            "kind": "Exposure", "exposureSettings": settings,
        })
    # .comp 的列表从底到顶。曝光位于工具对比度、饱和度层下方。
    position = next((i for i, item in enumerate(layers)
                     if item.get("name") in (CONTRAST_LAYER_NAME, SATURATION_LAYER_NAME)), len(layers))
    layers.insert(position, layer)


def set_contrast_layer(manifest, curves):
    """新增或复用独立曲线层；替换整组曲线，避免重复执行时累加。"""
    layers = manifest["layers"]
    matches = [layer for layer in layers if layer.get("name") == CONTRAST_LAYER_NAME]
    if len(matches) > 1:
        raise ValueError("存在多个 AI Contrast 图层，无法确定调整对象")
    if matches:
        layer = matches[0]
        adjustment = layer.get("adjustment")
        if (
            layer.get("isGroup") or "imageFile" in layer
            or not isinstance(adjustment, dict) or adjustment.get("kind") != "Curves"
        ):
            raise ValueError("AI Contrast 名称已被其他类型图层占用")
        adjustment["curves"] = curves
        layer["isVisible"] = True
        layers.remove(layer)
    else:
        layer = new_adjustment_layer(manifest, CONTRAST_LAYER_NAME, {
            "kind": "Curves", "curves": curves,
        })
    position = next((i for i, item in enumerate(layers)
                     if item.get("name") == SATURATION_LAYER_NAME), len(layers))
    layers.insert(position, layer)


def set_saturation_layer(manifest, amount):
    """新增或复用工具图层，只控制饱和度，不复制其他图层的蒙版和效果。"""
    matches = [layer for layer in manifest["layers"] if layer.get("name") == SATURATION_LAYER_NAME]
    if len(matches) > 1:
        raise ValueError("存在多个 AI Saturation 图层，无法确定调整对象")
    if matches:
        layer = matches[0]
        adjustment = layer.get("adjustment")
        if (
            layer.get("isGroup") or "imageFile" in layer
            or not isinstance(adjustment, dict) or adjustment.get("kind") != "Hue/Saturation"
        ):
            raise ValueError("AI Saturation 名称已被其他类型图层占用")
        adjustment["saturation"] = amount
        layer["isVisible"] = True
        # 工具管理的饱和度层固定放在最上方，保证作用于下方冷暖调整后的画面。
        manifest["layers"].remove(layer)
        manifest["layers"].append(layer)
        return

    manifest["layers"].append(new_adjustment_layer(manifest, SATURATION_LAYER_NAME, {
        "kind": "Hue/Saturation", "saturation": amount,
    }))


def validate_source_project(source_path):
    """只读取并检查原工程，返回路径、配置字典和目标图层，不写入文件。"""
    source = Path(source_path).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"找不到源工程目录：{source}")
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
    return source, manifest, target_layer


def get_project_image(source_path):
    """取得唯一、可见的顶层图片资源；不猜测多图或图层组的合成结果。"""
    source, manifest, _ = validate_source_project(source_path)
    if any(layer.get("isGroup") or layer.get("children") for layer in manifest["layers"]):
        raise ValueError("当前只支持没有图层组的单图片工程")
    images = [layer for layer in manifest["layers"] if "imageFile" in layer]
    if len(images) != 1:
        raise ValueError("工程必须恰好包含一个图片图层，不能自动选择多张图片")
    layer = images[0]
    layer_id = layer.get("id")
    if not isinstance(layer_id, str) or not layer_id:
        raise ValueError("图片图层必须有有效 ID")
    if sum(item.get("id") == layer_id for item in manifest["layers"]) != 1:
        raise ValueError("图片图层 ID 不唯一")
    if layer.get("isVisible") is not True:
        raise ValueError("图片图层必须可见")
    image_dir = (source / "images").resolve()
    image_path = (image_dir / layer["imageFile"]).resolve()
    if source not in image_dir.parents or image_dir not in image_path.parents:
        raise ValueError("图片资源必须位于工程内部的 images 目录")
    return source, layer, image_path


def create_adjusted_project(
    source_path, output_path, warmth_adjustment, saturation_adjustment=None, exposure_ev=None,
    contrast_adjustment=None,
):
    """检查输入、复制完整工程、修改副本，返回输出工程的绝对路径。"""
    settings = map_warmth_to_color_balance(warmth_adjustment)
    saturation = None if saturation_adjustment is None else map_saturation_adjustment(saturation_adjustment)
    exposure = None if exposure_ev is None else map_exposure_ev(exposure_ev)
    contrast = None if contrast_adjustment is None else map_contrast_to_curves(contrast_adjustment)
    requested_output = Path(output_path).expanduser()
    # 也拒绝失效的符号链接，避免意外写到链接指向的位置。
    if requested_output.exists() or requested_output.is_symlink():
        raise ValueError("输出路径已经存在，请换一个新的工程名称")
    output = requested_output.resolve()
    source, manifest, target_layer = validate_source_project(source_path)
    if output == source or source in output.parents:
        raise ValueError("输出不能是原始工程，也不能放在原始工程内部")
    if output.suffix != ".comp":
        raise ValueError("输出工程名称必须以 .comp 结尾")

    # 这里先修改内存中的字典；原始 manifest 文件没有被写入。
    target_layer["adjustment"]["colorBalanceSettings"] = settings
    target_layer["isVisible"] = True
    if exposure is not None:
        set_exposure_layer(manifest, exposure)
    if contrast is not None:
        set_contrast_layer(manifest, contrast)
    if saturation is not None:
        set_saturation_layer(manifest, saturation)

    # copytree 复制整个目录，包括图片和其他工程资源；默认拒绝覆盖。
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, output)
    with (output / "manifest.json").open("w", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, indent=2)
        file.write("\n")
    return output


def main():
    """把终端参数交给工具函数，并向用户报告执行结果。"""
    parser = argparse.ArgumentParser(description="复制工程并调整冷暖、可选饱和度、曝光和对比度")
    parser.add_argument("--input", required=True, help="原始 .comp 工程目录")
    parser.add_argument("--output", required=True, help="新的 .comp 工程路径，必须尚不存在")
    parser.add_argument("--warmth", required=True, type=float, help="冷暖值，范围 -1 到 1")
    parser.add_argument("--saturation", type=float, help="可选饱和度值，范围 -1 到 1")
    parser.add_argument("--exposure", type=float, help="可选曝光补偿，范围 -2 到 2 EV")
    parser.add_argument("--contrast", type=float, help="可选对比度建议值，范围 -1 到 1，非百分比")
    args = parser.parse_args()

    try:
        output = create_adjusted_project(
            args.input, args.output, args.warmth, args.saturation, exposure_ev=args.exposure,
            contrast_adjustment=args.contrast,
        )
    except (ValueError, OSError) as error:
        parser.exit(1, f"生成失败：{error}\n")

    print(f"已生成工程：{output}")
    print(f"冷暖参数：{args.warmth}；已启用图层：{TARGET_LAYER_NAME}")
    if args.saturation is not None:
        print(f"饱和度参数：{args.saturation}；已启用图层：{SATURATION_LAYER_NAME}")
    if args.exposure is not None:
        print(f"曝光补偿：{args.exposure} EV；已启用图层：{EXPOSURE_LAYER_NAME}")
    if args.contrast is not None:
        print(f"对比度建议值：{args.contrast}；已启用图层：{CONTRAST_LAYER_NAME}")
    print("请在 Compositor 中打开工程，开关调整图层检查效果。")


# 直接运行此文件时才启动命令行；其他模块导入函数时不会复制工程。
if __name__ == "__main__":
    main()
