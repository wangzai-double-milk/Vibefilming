---
name: skill-director
description: 当已有已定稿剧本或明确内容大纲，需要设计“怎么拍”时使用；也用于修复主线不清、镜头节拍混乱、资产漏项、空间/道具承接错误。负责产出或局部更新 director_plan，不负责写最终生成 prompt、直接生成素材或做审查放行。
---

# Skill · 编导转译

## 执行合同

- **触发**：剧本已定稿，准备进入资产、故事板、视频生成或合成；或审查结论指向 MAIN / INFO / REF / SPACE / CONT / PROP 等规划层问题。
- **输入**：已过审剧本、用户目标、时长与平台约束、现有 `director_plan.json`（返工时）。
- **唯一输出**：可执行的 `director_plan.json` 或对其中受影响部分的局部更新。
- **边界**：不重写剧本；不直接出图/出视频；不写工具字段；不替审查判通过。
- **完成标准**：下游不需要猜人物、场景、道具、镜头、节拍、承接、声音或参考素材职责。

## 每次执行只做这 5 步

1. **校准来源**：读已过审剧本和当前 plan；返工时先读审查结论，只改根因涉及的字段。
2. **钉主线与段功能**：写清“谁 + 目标/困境 + 阻碍/转折 + 落点”；每个 segment 说明如何推进主线。
3. **把信息变成镜头**：逐段写 beat、段内时间轴、不可丢信息、画面文字、声音事件、段初/段末状态。
4. **登记真实资产**：人物、场景多视角、道具、状态变体、关键特写、故事板、blocking、route map、色卡按需要登记；声明即承诺。
5. **做交付检查**：核对时长、镜头参数、资产覆盖、连续性、道具持有链和音频计划，明确“可进入下一阶段”或“仍阻塞”。

## `director_plan` 最低合同

计划至少包含以下信息；字段名可按项目现状调整，但语义不能缺：

- `story_spine`：一句话主线、结构选择、前三秒钩子。
- `style_contract`：媒介、写实程度、摄影显影、材质、负向禁忌、固定调色板。
- `reference_pack`：角色、场景、道具、状态变体、关键特写、色卡及其适用范围。
- `segments[]`：ID、功能、时长、beats、3-6 段时间轴、画面文字、声音、资产需求、故事板职责、段初/段末状态、终版状态。
- `continuity`：空间方向、人物左右关系、关键道具持有链、链式延展关系。
- `edit_plan` / `audio_plan`：切镜逻辑、转场理由、全片声音与 BGM 曲线。

## 硬门禁

以下任一项不满足，不得把任务交给素材或提示编译阶段：

- 没有一句话主线，或 segment 只是并列的漂亮画面。
- 文学剧本直接变生成 prompt，没有可执行 plan。
- 每段只有总体动作，没有镜头 beat 和 3-6 段时间变化。
- 逐字台词、旁白、关键文字、反转信息或道具状态没有落到可见/可听载体。
- 主要人物、场景、道具、状态变化或关键特写只靠文字，未登记真实参考资产。
- plan 声明了多视角、色卡、blocking、route map、状态变体，却未写清数量、用途和适用 segment。
- 人物穿场景、追逐或长距离移动，却没有场景多视角和 route map。
- 相邻连续段的地点、人物位置、光线、运动方向或道具状态无法首尾相接。
- 链式延展没有标明上一段过审终版及“从其末状态继续”的关系。
- 计划只写“电影感”“同上”“保持一致”等不可执行描述。

## 共用故障标签

| 标签 | 本阶段必须解决什么 |
|---|---|
| `MAIN-1 / HOOK-1 / INFO-1` | 主线、前三秒、关键叙事信息 |
| `REF-1 / STATE-1 / CU-3 / CAST-1` | 资产、状态变体、关键特写、角色记忆点 |
| `COLOR-1 / BG-1 / CU-2` | 固定调色板、参考与成片光影边界 |
| `TIME-1 / EMO-1 / CAM-1` | 段内变化、情绪链、可执行镜头机械动作 |
| `SPACE-1 / ROUTE-1 / CONT-1 / CHAIN-1 / PROP-1` | 空间、路线、段间承接、链式延展、道具持有链 |
| `STB-1 / STB-2 / CUT-1` | 故事板只管剧情流动，不发明资产、不决定切镜数 |
| `PHYS-1 / USE-1` | 生活物理、对象处理方式和人的本能反应 |
| `VOICE-1 / TXT-1 / CU-1` | 固定音色档案、逐字画面文字、文字载体特写 |

## 按需读取详细手册

不要默认把整本手册全部读进上下文。只在当前任务需要时，用 `keyword` 定位对应章节：

| 当前问题 | 读取命令 |
|---|---|
| 主线、钩子、信息、画面文字 | `file_read path="skills/skill_director/references/handbook.md" keyword="一、先定故事和结构" count=130` |
| 资产、状态、特写、角色、场景多视角 | `file_read path="skills/skill_director/references/handbook.md" keyword="三、资产与参考包规划" count=220` |
| 镜头 beat、时间轴、情绪 | `file_read path="skills/skill_director/references/handbook.md" keyword="四、镜头节拍表" count=120` |
| 镜头语言、空间、越轴 | `file_read path="skills/skill_director/references/handbook.md" keyword="五、镜头语言选择" count=90` |
| 故事板 | `file_read path="skills/skill_director/references/handbook.md" keyword="六、故事板" count=70` |
| 承接、道具、物理、剪辑、时长 | `file_read path="skills/skill_director/references/handbook.md" keyword="七、段间承接" count=145` |
| 人声与 BGM | `file_read path="skills/skill_director/references/handbook.md" keyword="八、音频计划" count=70` |
| 完整交付检查 | `file_read path="skills/skill_director/references/handbook.md" keyword="九、交付与放行自检" count=50` |

## 最终回报格式

执行结束只报告：

1. 更新了哪些 plan 部分。
2. 当前硬门禁是否全部通过。
3. 下一阶段应读取哪个 skill。
4. 若阻塞，缺的具体产物是什么。
