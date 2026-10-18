# Chromatic

Chromatic 是一个基于 Compositor 的 AI 照片调色学习项目，目标是让 AI 分析照片、解释调色建议，并生成可以继续编辑的调整结果。项目同时用于学习如何构建 Agent。

当前处于原型阶段：工程同源分析、参考作品双图分析、四项基础调色和连续用户反馈流程均已完成真实运行，并通过本次样片验收。47 项离线测试通过；首次反馈调冷过多，第二次明确冷暖最终值为 0.1 后符合用户要求。交互界面和自动结果渲染尚未实现，参数幅度仍需后续校准。

## 功能目标与验收标准

### 自动初步调色（目标，尚未完成）

输入一张照片和修图要求，AI 分析曝光、白平衡、对比度及色彩，提出调整方案，由程序校验后应用到 Compositor 工程副本。

验收标准：程序输出调色建议和理由，并生成能在 Compositor 中打开的调整后工程；用户开关调整图层，能够检查前后变化，原始照片和工程得到保留。

### 参考作品调色（完整目标尚未完成）

输入原照片、已调色的参考作品和修图要求，AI 分析两张图片的差异，借鉴参考作品的冷暖、对比度、饱和度及阴影和高光色彩关系，制定适合原照片的调整方案。

第一版计划在请求中同时提供两张图片，无需先训练专用模型。照片的光线和内容存在差异，参考风格调色不承诺完全复制参考图的观感。

验收标准：程序说明借鉴了哪些色彩特征，并生成可编辑的调整后工程；用户通过前后对比和参考图，判断结果是否朝目标风格靠近。

当前已支持双图分析、参考风格描述及迁移理由，并初步执行冷暖、饱和度、曝光和对比度；本次样片的四项组合效果已通过用户检查，不代表其他照片的审美效果或完整风格迁移均已验证。

## 预期工作流程

这是待逐步实现的流程：

```text
照片 + 修图要求 + 可选参考图
  → 模型分析并提出方案
  → Python 校验参数
  → 调色工具写入 Compositor 工程副本
  → 打开或渲染工程，查看结果
  → 用户反馈，必要时继续调整
```

模型负责理解和建议，工具负责执行调整，Python 程序负责连接各步骤、处理错误和保存结果。

## 当前实现状态

| 部分 | 已实现 | 边界 |
| --- | --- | --- |
| 工程取图 | 统一入口只接收工程，从唯一可见的顶层图片图层读取资源 | 拒绝多图片、图层组及不支持的结构；不分析渲染后的合成画面 |
| 分析预览 | Pillow 读取 JPEG/PNG，校正 EXIF 方向，转换为 RGB/sRGB，长边最多 1600，JPEG 质量 90 | 原图不修改；拒绝透明像素、多帧和超出当前处理限制的图片 |
| 模型分析 | DeepSeek 返回中文分析及曝光、对比度、冷暖、饱和度建议 | 格式及数值检查不能保证审美正确 |
| 来源校验 | 分析记录绑定工程图片图层 ID、资源引用和原图 SHA-256；复制前后核对原图 | 检查字节及引用一致性，不认证模型建议，也不等同于合成画面一致 |
| 调整执行 | 工程副本中设置 Color Balance、AI Exposure、AI Contrast 和 AI Saturation 图层，保存执行记录 | 不自动渲染或评估；本次样片的四项执行已验收，参数映射仍需后续校准 |
| 参考作品 | 可选 JPEG/PNG 参考、双预览、风格描述和迁移理由，已完成真实运行 | 本次四项基础执行已验收，不代表完整风格迁移 |
| 用户反馈 | 读取上轮报告与分析，结合文字反馈生成完整新方案，从同一原工程生成新版本 | 连续反馈及第二次回调已通过本次样片验收；模型未收到调整后成片，不自动评估结果 |

这是由 Python 安排顺序的固定工作流程；现支持人工反馈后再次请求模型，但模型暂不自行选择工具或观察渲染结果继续迭代。

## 环境与输入工程

使用 Python 3.11 或更高版本，uv 管理环境。依赖为 `openai`、`python-dotenv` 和 `pillow`，确切版本记录在 `uv.lock` 中。

```bash
uv sync
```

在项目 `.env` 中配置自己的 `DEEPSEEK_API_KEY`。脚本只读取该凭证，缺失时停止，不回退使用 `OPENAI_API_KEY`；已设置的系统环境变量优先。实际服务地址是 `https://api.deepseek.com`，当前模型为 `deepseek-flash`。密钥不得提交到 Git。

Compositor 工程需要：

- 包含且仅包含一个可见的顶层图片图层，图片位于工程内部的 `images/` 中，具有唯一有效 ID。
- 没有图层组；其他图片图层即使隐藏也不自动忽略。
- 包含且仅包含一个名为 `Test Color Balance` 的 Color Balance 调整图层。
- 图片资源必须能解码为 JPEG 或 PNG，当前最多 8000 万像素。

本地正确示例为 `outputs/test-photo-source.comp`，画面是红色遮阳伞和冰柜，图片图层名为 `test`。工程及照片未纳入版本管理，克隆仓库后需自行准备对应工程。本阶段读取原始图片资源，不处理裁剪、蒙版、多个图层、已有调整形成的最终合成观感。

## 推荐入口：从同一工程分析并调色

```bash
uv run run_agent.py --input outputs/test-photo-source.comp --instruction "保持自然，稍微暖一点，不要过度饱和" --output outputs/my-project-run
```

统一入口不再接受 `--image`，避免分析照片和执行工程被分别选成不同画面。`--output` 是一个全新的结果目录，必须尚不存在。运行会上传分析预览到 DeepSeek，并产生 API 用量。

成功结果结构：

```text
outputs/my-project-run/
├── analysis-preview.jpg
├── analysis.json
├── result.comp/
└── result.report.json
```

四个阶段依次是：检查输入与生成预览、请求分析、校验并保存分析、核对工程来源并执行冷暖、饱和度、曝光和对比度。预览的完整字节同时用于保存和模型请求，方便人工核对实际输入。

原工程中的完整图片继续用于输出工程；分析预览只用于模型，不替换工程原图。原图与 JPEG 预览分别记录指纹，不能直接把两种文件的指纹互相比较。PNG 文件超过旧的 32 MiB 限制时，可以先在本地解码并缩小，上传的只是小预览。

本次实际结果在 `outputs/run-project-source/`：44.41 MiB 的 `4672 × 7008` PNG 转成 `1067 × 1600`、约 470 KiB 的 JPEG。模型建议曝光 `0`、对比度 `0`、冷暖 `0.15`、饱和度 `-0.1`；实际只执行冷暖，映射幅度 `3`。此数值不是通用预设。

请求沿用 90 秒网络超时、最多 4096 个输出 token、关闭 SDK 自动重试和非思考模式。网络超时不是整个命令的精确总时限。接口参考 [DeepSeek 视觉文档](https://api-docs.deepseek.com/guides/vision/) 与 [思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/)。预览处理使用 [Pillow Image](https://pillow.readthedocs.io/en/stable/reference/Image.html) 和 [ImageOps](https://pillow.readthedocs.io/en/stable/reference/ImageOps.html)。

## 可选参考作品：双图分析与基础调整

```bash
uv run run_agent.py --input outputs/test-photo-source.comp --reference model.jpg --instruction "参考作品的暖色和暗部氛围，保留原图主体细节，不要为了接近参考图而过度压暗。" --output outputs/my-reference-run
```

`--reference` 可省略，省略时继续使用原有自动分析模式；指定时必须是能读取的 JPEG/PNG，截图也可使用。参考图缺失或无法解码时，在模型调用前停止。

模型消息明确标注图片 A 为待调整原图、图片 B 为仅借鉴风格的参考作品。原图仍只能来自工程，参考图不进入或替换输出工程。提示词要求区分场景和光线差异，针对原图制定方案，不机械复制参考图的亮度，也不复制主体或构图。

参考模式在结果目录增加 `reference-preview.jpg`。两份预览都使用已有预处理，并保存与实际发送内容相同的字节。分析元数据增加 `analysis_mode: reference` 及 `reference` 来源，包括参考原文件路径及指纹、预览路径及指纹、原始和预览尺寸。

分析新增两个字段，都会校验并保留到保存结果及执行报告：

- `reference_analysis`：`warmth`、`contrast`、`saturation`、`tone` 四项非空中文描述。
- `transfer_explanation`：说明原图与参考图的差异、借鉴特征、保留细节及局限的非空文字。

参考模式缺少上述字段时不执行调色；参考内容缺少有效参考来源记录时也拒绝执行。执行记录保存请求时的参考快照信息，重试执行不需要参考原文件仍在原位置，也不会重新请求模型。

本次实际结果在 `outputs/reference-model-run/`：原图预览 `1067 × 1600`，参考预览 `900 × 1600`。模型提出曝光 `-0.1`、对比度 `0.15`、冷暖 `0.25`、饱和度 `0.1`；仅执行冷暖，幅度 `5`，其余三项保留为未执行。本次模型曝光文字称保持曝光而数值为轻微降低，语义一致性仍需人工检查，字段校验不检测这类审美或文字判断。

上述冷暖版本已获用户确认。新增饱和度后的检查用结果为 `outputs/reference-saturation.comp`，继续使用相同分析，没有重新调用模型；冷暖 `0.25` 映射为 `5`，饱和度 `0.1` 映射为 `10`，曝光和对比度未执行。对应 `outputs/reference-saturation.report.json` 保留参考风格描述及迁移理由。

冷暖与饱和度版本已获用户确认。新增曝光版本为 `outputs/reference-exposure.comp`，继续使用同一分析，执行冷暖 `0.25 → 5`、饱和度 `0.1 → 10` 和曝光 `-0.1 EV`；对比度 `0.15` 未执行。对应 `outputs/reference-exposure.report.json` 保留参考风格描述、迁移理由和来源，曝光已获用户确认。最新四项执行结果为 `outputs/reference-contrast.comp` 及对应报告，增加对比度 `0.15`，四项组合效果已通过用户验收。旧结果与报告保留当时的执行范围。

## 根据用户反馈调整一次

查看四项组合结果后，可以引用其执行报告，提出修改要求。此入口需要上次四项均已执行的新版报告，不能使用早期只执行部分参数的报告。

```bash
uv run refine_agent.py --previous-report outputs/reference-contrast.report.json --input outputs/test-photo-source.comp --feedback "有点太暖了，稍微冷一点，其他尽量保持。" --output outputs/my-feedback-run
```

`--input` 必须是上一轮报告绑定的原工程，不能传入上一轮调色结果；`--output` 必须是新目录，且不能位于原工程或旧结果工程内部。示例中的原工程、照片及报告属于本地材料，未包含在仓库中。运行会发送原照片预览、可选参考预览、原修图要求、上一轮方案和反馈到 DeepSeek，产生 API 用量；模型调用参数继续使用 90 秒网络超时、关闭自动重试及原有输出限制。

入口先核对报告与分析的 SHA-256、原照片指纹与图层引用、完整四项已执行参数、旧结果工程原图、主要工具参数及顺序。若发现缺失、冲突、变化或损坏，在模型请求前停止；这里不是对整个合成画面的认证。文件记录仍用已有的 `project_created_pending_visual_review` 状态，表示生成成功但不自动认定审美通过，既往人工验收记录在 README 中。

原图和参考图都复用上次保存的 JPEG 预览，核对指纹后直接发送并复制到新目录，不重新压缩。参考原文件可以已删除，但上轮保存的参考预览必须存在且指纹一致。当前记录使用绝对路径；移动原工程、报告依赖或预览后可能需要重新建立有效记录，不自动猜测替代文件。

反馈提示词明确说明：模型没有收到调整后的成片，用户文字是对结果的人工观察。模型返回相对于同一原图的四项完整最终参数，不是增量；未涉及的参数尽量保持，并额外给出 `feedback_explanation`。输出仍需原有基础分析，参考模式仍需参考风格描述与迁移理由；缺字段、非法数值、空理由或无效 JSON 时不执行。

新目录包含 `analysis-preview.jpg`、可选 `reference-preview.jpg`、`analysis.json`、`result.comp` 和 `result.report.json`。分析元数据增加：

- `workflow: feedback`，`analysis_mode` 继续区分自动与参考模式。
- `feedback.text`：本次反馈。
- `feedback.previous_report_path` / `previous_report_sha256`：上轮报告位置与指纹。
- `feedback.previous_analysis_path` / `previous_analysis_sha256`：上轮分析位置与指纹。
- `feedback.previous_output_project` 和 `previous_adjustment_plan`：上轮结果与参数快照。

原始 `instruction` 继续保存最初修图要求。执行报告保留反馈来源、模型修改理由、`parameter_changes`（每项 previous/new/delta）和 `model_observation_scope`。其中 delta 用于查看变化，不传给工具叠加。保存分析后、执行前再次核对上一轮状态，然后调用现有执行函数从原工程生成副本，旧结果不覆盖。执行失败时保留新分析，核对原因后可使用独立执行入口重试；重试会保留反馈记录，不重新请求模型，也不重新认证旧成片。

本次真实调用结果位于 `outputs/feedback-run/`：

| 参数 | 上轮 | 本轮 |
| --- | --- | --- |
| 冷暖 | `0.25`（映射 `5`） | `-0.1`（映射 `-2`） |
| 曝光 | `-0.1 EV` | `-0.1 EV` |
| 对比度 | `0.15` | `0.15` |
| 饱和度 | `0.1` | `0.1` |

首次反馈的模型选择从偏暖跨到略偏冷，理由中明确未见成片。程序已验证来源、预览、四项设置与报告一致，并确认原工程、旧结果、参考图和旧分析保持原样。用户视觉检查指出冷调过多，其他参数符合要求；这说明参数合法性校验不保证对“稍微”等描述的幅度判断准确。

随后使用同一反馈入口，引用 `outputs/feedback-run/result.report.json`，明确将冷暖最终值设为 `0.1`，其他三项保持不变，生成 `outputs/feedback-warmth-rebound/`。本轮冷暖从 `-0.1` 回调为 `0.1`（编辑器映射 `+2`），曝光仍为 `-0.1 EV`、对比度 `0.15`、饱和度 `0.1`。执行记录保留上一轮来源，参数与实际图层配置核对一致，用户已检查并确认符合要求。

这一过程验证了连续反馈的来源衔接和其他参数保持不变；每轮仍从同一原工程生成新副本，前两个版本保留。验收结论限于本次样片与指定参数，自动理解调整幅度和参数映射的精细校准留在第二阶段。

这一版使用文件保存状态，未引入数据库、聊天界面或自动观察成片的循环。

## 分析与执行记录

`analysis.json` 包含：

- `metadata`：工程图片路径、原图 SHA-256、原图尺寸、预览路径、预览 SHA-256、预览尺寸和处理方式、修图要求、模型及 UTC 时间。
- `metadata.project_source`：原工程位置、图片图层 ID 和资源引用。
- `analysis`：曝光、白平衡、对比度、整体色彩倾向的非空中文分析。
- `adjustment_plan`：四项合法有限数值，曝光范围 `[-2, 2]`，其余范围 `[-1, 1]`。

新版 `result.report.json` 记录原图和分析文件的来源与指纹、原图层引用、四项已执行参数、实际工具设置（含对比度的四通道曲线）、工具图层名、从底到顶的工具图层顺序和映射规则。当前四项均执行，`not_applied_adjustments` 为空对象，原因字段说明没有未执行的基础建议。`project_photo_verified: true` 表示工程资源与分析来源通过了字节和引用核对，**不表示审美或工程合成画面已验收**。状态 `project_created_pending_visual_review` 表示等待用户视觉检查。

文件指纹用于发现误选和内容变化，不是对手动编辑分析文件的真实性认证。

## 独立入口

只分析 JPEG/PNG，不生成调色工程：

```bash
uv run main.py --image test.jpg --instruction "保持自然，稍微暖一点" --output outputs/standalone-analysis.json
```

该入口使用相同的预览准备逻辑，但没有工程来源绑定，其结果不能交给新版执行入口直接调色。需要调色时使用统一入口重新生成工程绑定分析。

使用新版已保存分析重试执行，无需再次调用模型：

```bash
uv run apply_analysis.py --analysis outputs/run-project-source/analysis.json --input outputs/test-photo-source.comp --output outputs/retry-result.comp
```

执行入口也不再接受 `--image`，而是从目标工程取图进行核对。缺少工程绑定或预览指纹的旧分析、图层引用不一致、图片内容不一致，都会被拒绝。工程和旁边的报告路径均须尚不存在。

人工指定冷暖的工具入口仍保留，用于独立实验：

```bash
uv run compositor_tool.py --input outputs/test-photo-source.comp --output outputs/manual-warm.comp --warmth 0.5
```

它只验证参数和工程结构，不使用 AI 分析。正值预期偏暖，负值预期偏冷，映射幅度为 `round(warmth * 20)`。这是用于打通流程的实验映射，尚未校准，不能视为精确的色温转换；第二阶段再研究响应并改进。参数替换目标图层的设置，不是在原值上累加；零值只中和此调整图层。

## 基础饱和度执行

工具入口支持可选 `--saturation`，旧的仅冷暖调用继续兼容，省略该选项时不新增或修改饱和度图层。已有分析执行入口和统一入口现在执行方案中的四项基础调整。

```bash
uv run compositor_tool.py --input outputs/test-photo-source.comp --output outputs/manual-saturation.comp --warmth 0 --saturation -0.3
```

饱和度建议范围 `[-1, 1]` 通过 `round(value * 100)` 对应编辑器 `[-100, 100]`。这是范围换算，不是视觉百分比，也没有完成样片校准。字段和图层类型依据 [Compositor 官方工程写入说明](https://github.com/robbietilton/Compositor/blob/main/docs/writing-comp-files.md) 与 [参数格式说明](https://github.com/robbietilton/Compositor/blob/main/docs/project-format.md)。

输出新增 `AI Saturation` 图层，类型为 `Hue/Saturation`，色相和明度为零，着色关闭，Levels 和 Curves 为中性。新图层使用独立 UUID，没有图片文件，位于最上方，作用于下方冷暖及可选曝光、对比度调整后的画面。原图片资源不改动。

若源工程已有工具管理的同名 Hue/Saturation 图层，则保留其 ID、替换 saturation、启用并移到最上方，不再新增一层，也不叠加旧数值；已有的其他设置保留。同名图层重复或类型冲突时停止，不覆盖其他图层。

已生成检查用工程，三份都将冷暖设为零，以便单独比较饱和度：

| 文件 | 建议值 | 写入值 |
| --- | --- | --- |
| `outputs/saturation-tests/decrease.comp` | `-0.3` | `-30` |
| `outputs/saturation-tests/neutral.comp` | `0` | `0` |
| `outputs/saturation-tests/increase.comp` | `0.3` | `30` |

先在 Compositor 中开关各自的 `AI Saturation`，确认降低、零值、增加的方向和效果。再打开 `outputs/reference-saturation.comp`，分别开关 `Test Color Balance` 和 `AI Saturation`，检查合并效果。结构检查及用户视觉验收已通过；目前尚无自动导出的成片 JPG/PNG。

## 基础曝光执行

独立工具新增可选 `--exposure`，单位为 EV（曝光补偿档数），范围限制为 `[-2, 2]`。省略时不新增或修改曝光层；显式传 `0` 时生成或重置中性曝光层。原来的冷暖及饱和度调用继续兼容。

```bash
uv run compositor_tool.py --input outputs/test-photo-source.comp --output outputs/manual-exposure.comp --warmth 0 --exposure 0.5
```

已核对 Compositor 公开源码，固定版本为 `365b5745eda537f984f3d5bed1e53c361dbc9c91`：[LayerAdjustment.swift](https://github.com/robbietilton/Compositor/blob/365b5745eda537f984f3d5bed1e53c361dbc9c91/Compositor/Document/LayerAdjustment.swift) 定义 `kind: Exposure` 和 `exposureSettings`；[ImageAdjustments.swift](https://github.com/robbietilton/Compositor/blob/365b5745eda537f984f3d5bed1e53c361dbc9c91/Compositor/Document/ImageAdjustments.swift#L35-L70) 定义 `exposure`、`offset`、`gamma`。曝光按 `2 ** exposure` 缩放线性光，再编码回显示值；编辑器曝光范围为 `[-20, 20]`，本项目仍限制在 `[-2, 2]`。这次只使用 EV，固定 `offset=0`、`gamma=1`。EV 不乘系数、不四舍五入，也不等同于直接乘 sRGB 像素数值；增加曝光可能使高光达到上限。公开源码核实不替代本机 Compositor 的实际加载与视觉验收。

新增 `AI Exposure` 独立调整层，具有唯一 UUID、全画布尺寸、Normal 混合、100% 不透明度，以及中性的 Levels、Curves 等基础设置。`new_adjustment_layer` 抽出曝光与饱和度共用的建层结构；实际像素处理交给 Compositor。

当前四项组合执行的工具顺序从底到顶为 `Test Color Balance → AI Exposure → AI Contrast → AI Saturation`；省略对比度的旧工具调用不新增该层。若已有同名 Exposure 层，复用 ID、启用并替换 `exposureSettings`，将 offset 和 gamma 恢复中性，不累加 EV；其他图层属性保留。同名重复、组或类型冲突时拒绝复制。当前是无蒙版、全画布工具层的基础执行方案，不承诺对用户自行改过蒙版、不透明度等配置仍产生相同效果。

检查用固定工程都从原始冰柜工程生成，冷暖为零，不新增饱和度层，原有 Test Curves 保持关闭：

| 文件 | 曝光 |
| --- | --- |
| `outputs/exposure-tests/decrease.comp` | `-0.5 EV`，预期变暗 |
| `outputs/exposure-tests/neutral.comp` | `0 EV`，预期保持 |
| `outputs/exposure-tests/increase.comp` | `+0.5 EV`，预期变亮 |

随后用已有参考分析执行三项调整，没有再次请求模型：

```bash
uv run apply_analysis.py --analysis outputs/reference-model-run/analysis.json --input outputs/test-photo-source.comp --output outputs/reference-exposure.comp
```

以上命令本次已经执行，输出和报告已存在，重试时须换新名称。已核对四份输出的参数、图层类型、唯一 ID、资源完整性及报告一致性；原工程、参考图和旧分析保持原样。曝光实际加载与效果已获用户确认。该版本保留三个工具层，`-0.1 EV` 的组合变化较轻微；新增对比度的四项组合验收见下节。

## 基础对比度执行

独立工具新增可选 `--contrast`，建议值范围为 `[-1, 1]`，不是百分比。省略时不新增或修改对比度层；显式 `0` 时创建或重置为中性曲线。旧的冷暖、饱和度和曝光调用继续兼容。

```bash
uv run compositor_tool.py --input outputs/test-photo-source.comp --output outputs/manual-contrast.comp --warmth 0 --contrast 0.5
```

参数结构与插值方式依据固定版本的 [Compositor Curves.swift](https://github.com/robbietilton/Compositor/blob/365b5745eda537f984f3d5bed1e53c361dbc9c91/Compositor/Document/Curves.swift)：`curves.channel` 为 `RGB`，`channels` 依次是 RGB 总曲线、红、绿、蓝。每条曲线需有 2～32 个点，起止横坐标为 0 和 255，横坐标严格递增，横纵坐标均为有限数值且在 `[0, 255]` 内；允许小数。编辑器以保形三次 Hermite 插值平滑连接控制点，避免过冲。像素先经过单独颜色通道曲线，再经过 RGB 总曲线。

本项目只调整总曲线，单独红、绿、蓝保持中性。暂定幅度 `a = contrast_adjustment * 32`，不取整；总曲线的五个点为：

```text
(0, 0), (64, 64-a), (128, 128), (192, 192+a), (255, 255)
```

正值压低暗部控制点并抬高亮部，负值反向；黑、白及中间控制点固定。零值为中性直线。全范围内控制点纵坐标也严格递增。这是有边界的基础 S 曲线方案，未完成样片校准；`0.15` 不代表视觉对比度增加 15%。RGB 总曲线逐通道处理仍可能改变颜色观感，也不代表感知亮度保持不变。保留纯黑与纯白，因此降低对比度不会把纯黑抬成灰。

新增独立 `AI Contrast` 曲线层，置于曝光之上、饱和度之下。沿用已有建层函数；不改动原来的 `Test Curves`。已有同名 Curves 层则复用 ID、替换整组曲线、恢复单独颜色通道中性，并启用该层，不累加；其他图层属性保留。同名重复、组、图片层或类型冲突时拒绝生成。与其他工具层一样，当前按未被用户改过蒙版和不透明度的全画布层验收。

三份固定参数工程从原始冰柜工程生成，冷暖为零，不新增曝光和饱和度层，原来的 Test Curves 保持关闭：

| 文件 | 建议值 | 暗部点 / 亮部点 |
| --- | --- | --- |
| `outputs/contrast-tests/decrease.comp` | `-0.5` | `64 → 80` / `192 → 176` |
| `outputs/contrast-tests/neutral.comp` | `0` | `64 → 64` / `192 → 192` |
| `outputs/contrast-tests/increase.comp` | `0.5` | `64 → 48` / `192 → 208` |

使用同一份已有参考分析完成四项执行，没有再次调用模型：

```bash
uv run apply_analysis.py --analysis outputs/reference-model-run/analysis.json --input outputs/test-photo-source.comp --output outputs/reference-contrast.comp
```

该命令本次已执行，重试须换新的输出名称。组合参数为冷暖 `0.25 → 5`、曝光 `-0.1 EV`、对比度 `0.15`（暗部 `64 → 59.2`，亮部 `192 → 196.8`）和饱和度 `0.1 → 10`。对应 `outputs/reference-contrast.report.json` 记录四项参数、实际曲线、图层顺序及参考来源；未执行项为空，不表示完整风格迁移已经完成。

在此前对比度步骤的 36 项 Python 回归测试之外，还在临时目录直接运行了公开源码的 Swift 曲线校验与插值方法：对 `[-1, 1]` 每隔 `0.01` 的 201 组强度，每组以 `0.1` 间隔采样 0～255，共 512,751 个采样点，确认曲线有效、方向正确、单调且不越界，零值为中性。该核对使用官方曲线计算方法，不等同于运行整套 Compositor 图像渲染，也不新增项目运行依赖。

四份输出已核对资源和配置，原工程、参考图及分析文件保持原样。请先开关三份固定工程的 `AI Contrast`，检查降低、零值、增加的方向；再打开组合工程，分别开关四个工具层。三份固定参数工程和四项组合工程的实际加载及效果已通过用户验收；此结论限于本次样片和检查参数，目前仍无自动导出的成片 JPG/PNG。

## 检查和失败处理

运行离线回归测试，不产生 API 用量：

```bash
uv run python -m unittest discover -s tests -v
```

已保存的 47 项离线测试通过：原有 36 项包括 10 项照片来源、7 项参考作品、6 项饱和度、6 项曝光及 7 项对比度测试；新增 11 项反馈测试，覆盖请求上下文、预览原字节、完整最终参数、自动与参考模式、来源不匹配、缺失元数据、旧工具变化、非法响应、请求期间原图变化，以及反馈来源在保存和重试中的保留。所有测试均使用模拟模型，不产生 API 用量。

本次同源真实运行的程序检查通过：原图、分析记录和输出图片指纹一致，图层 ID 和资源引用一致，保存预览与预览指纹一致；配置只改变目标调整图层，原工程、原照片及旧分析均保留。用户已打开最新结果并确认验证通过；这不代表其他照片或其他调整参数也已验证。

参考模式冷暖版本的程序检查和用户验收已通过。本次新增饱和度没有重新请求模型，三份固定参数工程及一份已有分析驱动工程的结构检查通过：仅目标冷暖配置与饱和度层变化，资源完整，原工程、照片、参考作品及分析文件均未改变。新增饱和度效果已获用户确认。

用户验收顺序：先查看 `analysis-preview.jpg`，再阅读分析，最后打开 `result.comp`，确认是同一画面并开关 `Test Color Balance` 检查效果。Finder 缩略图可能是旧预览，以 Compositor 实际画布为准。

输入不符合要求时，在请求模型前停止。请求或分析失败不继续调色；已创建的预览和部分结果保留，只有本次创建且仍为空的目录才会移除。执行失败时保留分析，可用独立执行入口重试。输出不覆盖，失败后重试应选择新目录或新的输出工程名称。

工程与报告尚未整体原子写入。复制后还会检查输出图片指纹；若不一致，不保存成功报告，并保留部分输出供检查。文件没有被全程锁定，这不是并发编辑工程的支持承诺。

## 图片错配事件与历史记录

早期命令分别指定 `test.jpg` 和 `../demo.comp`，前者是红色遮阳伞及冰柜，后者是海边猫照片。代码当时只核对 JPEG 与分析记录，未绑定工程图片，因此把冰柜的建议应用到了猫照片上。早期工具变色和模型分析的验证不能当作同一照片完整流程的验收。

按用户确认，已删除五份生成的猫照片工程，原始 `../demo.comp` 保留。旧的分析及执行记录保留用于追溯，其引用的测试工程可能已删除；旧记录中的 `project_photo_verified: false` 不是新版来源检查通过的记录。`manifest_test.json` 和 `inspect_compositor.py` 属于早期实验，不作为当前推荐入口。

正确的冰柜源工程已建立，用户确认可以正常显示。随后统一入口改为工程唯一来源，并新增以上回归测试；本次最新结果也已通过用户视觉验收。

## 文件职责与版本管理

| 文件 | 职责 |
| --- | --- |
| `main.py` | JPEG/PNG 预览、模型请求、结果校验和保存 |
| `compositor_tool.py` | 工程检查、唯一图片选择、四项基础调整映射和工程副本 |
| `apply_analysis.py` | 已存分析与目标工程来源校验、执行记录 |
| `run_agent.py` | 初次分析与调色的统一四阶段流程、通用错误说明 |
| `refine_agent.py` | 核对上一轮状态、请求反馈方案、从原工程生成新版本 |
| `tests/test_project_source.py`、`tests/test_reference.py`、`tests/test_saturation.py`、`tests/test_exposure.py`、`tests/test_contrast.py`、`tests/test_feedback.py` | 来源、参考作品、基础工具及用户反馈的离线回归测试 |
| `AGENTS.md` | 先讲解、批准后施工、验收后继续的协作规则 |
| `pyproject.toml`、`uv.lock` | 依赖声明与确切版本锁定 |

Git commit 在本地保存版本，Git push 将提交同步到 GitHub。每个阶段通过程序检查和用户验收后，提醒用户检查差异并提交；需要备份或分享时再推送。来源修正与统一入口此前已提交并推送；四项基础调色版本已保存为本地提交 `a4970b9`，尚未推送；本次整理用户反馈与连续回调功能，同样按用户授权仅创建本地提交，暂不推送 GitHub。实际提交与同步状态以 Git 记录为准。

`.gitignore` 排除 `.env`、`.venv/`、Python 缓存、`.DS_Store`、根目录 `test.jpg`、`manifest_test.json` 和 `outputs/`，忽略不会删除本地文件。`test.py` 是用户的 Base64 练习；其暂存状态保留，不自动混入阶段提交。

## 两阶段开发计划

### 第一阶段：跑通可演示的 Agent 任务流程

目标是从输入工程、修图要求及可选参考作品，完成分析、合法方案、工具执行、可编辑结果及执行记录，并支持用户查看结果后提出一次修改要求，再次调整。

- 已完成：工程唯一图片来源、分析预览、模型建议、来源校验及冷暖执行；已通过本次用户验收。
- 已获用户验收：参考作品双图分析，以及冷暖、饱和度、曝光和对比度四项基础执行；三份固定对比度工程与参考分析驱动的四项组合工程均通过本次用户检查。
- 已补齐当前方案的四项基础调色执行能力；继续使用有边界且可追溯的实现，参数执行完整不代表完整风格迁移。
- 已验收：用户反馈流程及连续回调。首次反馈偏冷过多，随后明确冷暖最终值 `0.1` 的版本符合要求；通过 JSON 保留各轮来源、反馈、方案及执行内容。
- 接下来增加交互界面，方便选择输入、查看建议、打开工程及提交反馈。
- 阶段内优先保证任务完整、输入对应、执行准确和失败可解释；不反复追求每项参数的精细效果。

当前 `乘以 20` 的冷暖幅度只是明确标记的暂定规则，第一阶段不把它当作已经校准的精确转换。

### 第二阶段：改进调色质量和参数转换

- 查清 Compositor 各参数的含义、取值范围及实际响应。
- 使用多张样片校准调整幅度，检查冷暖、曝光、对比度和饱和度映射。
- 根据验证结果替换线性映射，必要时重新设计模型输出的参数含义。
- 进一步处理高光、中间调、阴影、肤色及参考风格迁移质量。
- 在能够可靠查看渲染结果的基础上，再讨论效果评估和自动迭代。

阶段目标仍是在范围受控的前提下完成可演示版本，同时逐步理解实现；后续功能每一步先讲解，经用户批准后施工。
