---
name: skill-prompt-engineering
description: 仅在即将调用图片、视频或音乐生成模型时使用。负责把已定稿的 director_plan、剧本文字和终版参考资产编译成“本次调用”的完整生成要求并做调用前自检；不负责重新规划剧情、补发明资产、调用工具或审查生成结果。
---

# Skill · 生成要求编译

## 执行合同

- **触发**：下一步就是一次正式的生图、生视频或音乐生成调用。
- **输入**：`director_plan` 当前对象/segment、剧本逐字文本、实际存在且已过审的参考素材、真实调用设置。
- **输出**：一次调用的一份完整生成要求 + 参考绑定表 + 明确自检结论。
- **边界**：不改剧本，不改 plan，不发明缺失资产，不写工具名/字段/路径/轮询，不替审查放行。
- **失败回流**：缺 plan 回编导；缺参考回素材；生成结果有问题按审查标签回根因层；禁止原样重抽。

## 每次调用只做这 6 步

1. **锁定单次目标**：明确本次生成什么对象、对应哪个 segment/beat、目标时长/画幅/声音设置。
2. **读取权威来源**：以 `director_plan` 为主，剧本逐字文本为补充；冲突时停止，先回上游校准。
3. **列参考绑定表**：每张图/视频分别锁身份、空间、材质、状态、特写、色彩、剧情流动或运动路线；没有实际输入就不能声称“已参考”。
4. **编译正文**：中文写清镜头、主体、材质、空间、空气、光、颜色；视频再写 3-6 段时间轴与声音变化。
5. **核对真实设置**：正文与实际时长、画幅、音频开关、参考输入不冲突。
6. **先判再调用**：输出 `pe_self_check / model_call_preflight: PASS | BLOCKED`；至少覆盖 `time_axis_motion`、`expression_and_secondary_motion`、`prompt_specificity`、`blocking_findings`。BLOCKED 时指出回哪个上游补什么，不得先调用再补理由。

## 一次生成要求的最低合同

- **参考绑定表**：每个实际输入写成 `参考 A = <对象/文件语义>；只锁 <职责>；不继承 <禁止维度>`。故事板只锁剧情流动；blocking / route map 只锁站位或路线；色卡只锁颜色；参考视频只锁声明的连续性、运动、节奏、特效或音色。
- **正文七层**：镜头、主体、材质、空间、空气、光、色彩均须具体；plan 已给出的数字和调色板逐字保留。
- **视频增量**：必须写 3-6 段时间轴、段初/段末状态、道具持有链、物理与本能预动作、逐字声音/文字及其出现时机。

## Seedance 特殊符号

写视频生成正文时保留这组语法，符号内只放对应内容：

- `（音乐描述）`：音乐 / BGM。
- `<具体音效>`：音效 / 环境声。
- `{逐字台词}`：台词 / 实声对白，并在前文写清说话人和固定音色。
- `【逐字画面文字】`：标题标识 / 画面文字 / 屏幕叠字。

不要混用符号，也不要把工具参数写进符号轨。

## 硬门禁

以下任一项出现，结论必须是 `阻塞`：

- 只写“电影感、保持一致、参考上图、同上、承接上段”等空引用。
- plan 的 beat、镜头数字、台词、画面文字、段初/段末状态或道具持有链被删改。
- 声称参考某资产，但实际参考输入中没有它，或未说明职责。
- 本段需要的人物、场景、道具、状态变体、关键特写、故事板或路线图尚未生成/过审。
- 故事板被当成镜头数、人物细节、材质、场景结构或视角的权威。
- 链式延展没有真实使用上一段过审终版视频，并在正文开头写“基于参考视频继续往后扩写”；还要说明参考视频最后状态以及人物、服装、道具、光线、机位、运动方向均不重置。
- 视频只有总体动作链，没有分阶段变化。
- 调色板未逐字复述，或当前 prompt 自行改色。
- 真实调用设置与正文冲突，例如写有对白却关闭音频。
- prompt 出现工具名、参数字段、项目路径、任务轮询或 SDK 实现细节。

## 共用故障标签

优先使用：`REF-1`、`STATE-1`、`CU-3`、`COLOR-1`、`TIME-1`、`VOICE-1`、`TXT-1`、`PHYS-1`、`USE-1`、`SPACE-1`、`ROUTE-1`、`CONT-1`、`CHAIN-1`、`PROP-1`、`FIX-1`。

## 按需读取详细手册

不要默认读取整本。只按当前调用类型定位：

| 当前问题 | 读取命令 |
|---|---|
| 上游消费、人物标签、台词、文字、故事板、参考职责 | `file_read path="skills/skill_prompt_engineering/references/handbook.md" keyword="一、先消费上游" count=210` |
| 七层画面、时间轴、光影、镜头、空间、材质、情绪 | `file_read path="skills/skill_prompt_engineering/references/handbook.md" keyword="二、把方案编译成画面" count=260` |
| Seedance 特殊符号 | `file_read path="skills/skill_prompt_engineering/references/handbook.md" keyword="三、Seedance 特殊符号" count=45` |
| prompt 写法和调用前流程红线 | `file_read path="skills/skill_prompt_engineering/references/handbook.md" keyword="四、生成要求怎么写" count=75` |
| 工具层边界 | `file_read path="skills/skill_prompt_engineering/references/handbook.md" keyword="五、不要写工具层内容" count=25` |
| 完整放行检查 | `file_read path="skills/skill_prompt_engineering/references/handbook.md" keyword="六、放行自检清单" count=55` |

## 调用前回报格式

只报告生成对象、权威来源、实际参考及职责、关键设置，以及第 6 步规定的四项自检证据；阻塞时补充原因和应回到哪个上游补什么。
