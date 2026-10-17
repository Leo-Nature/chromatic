import json
from pathlib import Path

# Compositor 项目的 manifest.json 路径
manifest_path = Path.home() / "workspace" / "demo.comp" / "manifest.json"

# 读取 JSON 文件，并转换成 Python 字典
with manifest_path.open("r", encoding="utf-8") as f:
    manifest = json.load(f)

# 遍历所有图层
for layer in manifest["layers"]:
    if layer["name"] in ("Test Curves", "Test Color Balance"):
        print(f"\n图层名称：{layer['name']}")
        print(f"图层 ID：{layer['id']}")
        print(f"当前是否可见：{layer['isVisible']}")

        # 只打印调整参数，不打印 transform 等无关信息
        print(json.dumps(
            layer["adjustment"],
            ensure_ascii=False,
            indent=2
        ))

def map_warmth_to_color_balance(warmth_adjustment):
    """
    将模型输出的 warmth_adjustment（-1 到 1）
    映射为 Compositor 的 Color Balance 参数。
    """

    if isinstance(warmth_adjustment, bool) or not isinstance(
        warmth_adjustment, (int, float)
    ):
        raise ValueError("warmth_adjustment 必须是数字")

    if not -1 <= warmth_adjustment <= 1:
        raise ValueError("warmth_adjustment 必须在 -1 到 1 之间")

    # 第一版采用简单的线性映射。
    # 这里的 20 是我们暂定的映射幅度，之后需要实际验证。
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


test_plan = {
    "warmth_adjustment": -0.1
}

mapped = map_warmth_to_color_balance(
    test_plan["warmth_adjustment"]
)

print("\n映射后的 Color Balance 参数：")
print(json.dumps(mapped, ensure_ascii=False, indent=2))


# 找到 Color Balance 图层
color_balance_layer = next(
    layer
    for layer in manifest["layers"]
    if layer["name"] == "Test Color Balance"
)

# 将映射结果写入图层配置
color_balance_layer["adjustment"]["colorBalanceSettings"] = mapped

# 启用这个调整图层
color_balance_layer["isVisible"] = True

# 将修改后的 manifest 保存为新文件，不覆盖原文件
output_path = Path("manifest_test.json")

with output_path.open("w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)

print(f"\n测试配置已保存到：{output_path.resolve()}")
