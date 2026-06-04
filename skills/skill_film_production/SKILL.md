---
name: skill-film-production
description: 做视频/短片/广告/MV/短剧/九宫格/竖屏成片/横屏成片/混剪/拼接成片——任何要产出一条完整视频的任务，**必须先 file_read 我读完，才能调任何工具**。我是制作短片的总指挥：从 project_create 到交付的 7 阶段管线、5 道硬闸门（参考图先行 / 调用前必过 PE / 逐段链式 / 每产物 VLM 审 / BGM 走后期）、每步该读哪个子 skill 都在我这里。不读我会漏 PE 和审片，出片必废。
---

# Skill · 制作短片（Film Production）—— 端到端总流程编排

> **何时读我**：任何"做一条短片 / 视频 / 广告 / MV / 短剧"的 brief 一进来，**第一个读我**。
> **我是什么**：总指挥。我不重复子 skill 的细节，只负责**串流程**——告诉你现在在哪一步、下一步干什么、该读哪个子 skill、哪一关绝不能跳。
> **核心心法**：你是导演，一开拍就一口气拍到交付，中途不停下问用户（见 [skill_self_decision](../skill_self_decision/SKILL.md)）。

---

## 全流程总览（7 阶段，按序推进，可被 VLM 反馈打回重做）

```
0. 建项目      project_create
1. 分镜先行    file_write storyboard.json        →读 skill_storyboard
2. 参考图先行  gen_image 出齐主体 + 逐张 VLM 审  →读 skill_entity_consistency + skill_director_vlm
3. 逐镜生成    gen_video_t2v（调用前过 PE！）     →读 skill_prompt_engineering + skill_video_chain
   ↘ 异步等待  query_video_task                  →读 skill_async_schedule
   ↘ 逐段审片  vlm_understand                    →读 skill_director_vlm
4. 配音配乐    对话/音效在生成阶段，BGM 走后期    →读 skill_audio
5. 合成成片    video_concat → gen_audio_bgm → audio_amix
6. 终审交付    成片 VLM 终审 + 竖屏/字幕等收尾
```

> ⚠️ **流程不写死**：VLM 反馈可让你跳回任一阶段重做某 shot、换思路。没有"必须一条道走到黑"，但**关卡（下面标 ⛔ 的）一个都不能跳**。

---

## 阶段 0 · 建项目

`project_create` 起一个项目，拿到 `pid`。之后所有中间物（分镜、prompt、图、视频、评估）都落盘到 `projects/<pid>/`。

## 阶段 1 · 分镜先行 ⛔

`project_create` 之后**第一件事不是出图**，是 `file_write` 把分镜写到 `projects/<pid>/storyboard.json`。
- ≥2 镜头必做；单镜头可省
- 分镜 = 交付契约：列了 4 段就要交 4 段，**不许偷偷砍**

→ **读 [skill_storyboard](../skill_storyboard/SKILL.md)**（字段结构 / self_review 5 条 / **并行 or 链式出片路线选择** / 关键帧并行法 / 任务类型三选一 / 4 类括号 / 高频踩坑）

## 阶段 2 · 参考图先行 ⛔（最常被跳，跳了必崩）

`storyboard.entities_planned` 里每个主体（角色/道具/场景），**先用 `gen_image` 出参考图**，**每张都过 `vlm_understand` 审**，审过了才进生成阶段。
- ❌ 严禁 `reference_images=[]` 靠文字脑补人物（已踩坑：环保广告跳过参考图，人物全跑偏）
- ❌ 参考图有缺陷不许在 shot prompt 里"打补丁"——回到 `gen_image` 把参考图改干净再用

→ **读 [skill_entity_consistency](../skill_entity_consistency/SKILL.md)**（白底三视图 / 锁参考 / 跨镜头一致性）
→ **读 [skill_director_vlm](../skill_director_vlm/SKILL.md)**（审片 question 三段式、别问"是否…"）

## 阶段 3 · 逐镜生成 ⛔（调用前必过 PE）

每个 shot 调 `gen_video_t2v`，**调用前最后一道关：过 Prompt Engineering checklist**。这一步最容易被略过——别略过。

- ⛔ **每次 `gen_image` / `gen_video_t2v` 调用前，先过 [skill_prompt_engineering](../skill_prompt_engineering/SKILL.md) 的 7 步 checklist**（任务类型 / 主体定义 / 动态描述顺序 / 4 类符号 / BGM 闸门 / 时长比例双保险 / 反例扫描）
- ⛔ **≥2 段视频必链式**：从第 2 段起 `gen_video_t2v` 必须传 `reference_video_url=上一段 url` → **读 [skill_video_chain](../skill_video_chain/SKILL.md)**
- 想固定机位传 `camera_fixed=true`；想复现/微调同一画面传同一 `seed`
- 提交后是异步任务，用 `query_video_task` 轮询拿结果 → **读 [skill_async_schedule](../skill_async_schedule/SKILL.md)**
- 每段视频落盘后 **`vlm_understand` 审片**，不过审 = hard stop，自己 PE 重写重做（最多 2 轮）→ [skill_director_vlm](../skill_director_vlm/SKILL.md)

## 阶段 4 · 配音配乐 ⛔（BGM 走后期）

- **对话 + 现场音效**：在 `gen_video_t2v(generate_audio=True)` 生成阶段产出（4 类符号 `{台词}` `<音效>`）
- **BGM 一律不让 Seedance 出**：走"成片完成 → `gen_audio_bgm`（BigMusic）出整片一首 → `audio_amix` 后期铺底"

→ **读 [skill_audio](../skill_audio/SKILL.md)**（BGM 后期路线 / reference_audios 共享 / MV 模式 / amix 后必审）

## 阶段 5 · 合成成片

1. `video_concat` 把审过的各段按分镜顺序拼起来（需转场用 `video_crossfade`，可选 `transition` 类型）
2. `gen_audio_bgm` 出整片 BGM（可用 `segments` 编排段落结构）→ `query_audio_task` 等结果
3. `audio_amix` 把 BGM 铺到整片底下
4. 头尾黑场 `video_fade` 收边

## 阶段 6 · 终审交付 ⛔

- 成片整体过一次 `vlm_understand` 终审（叙事完整？衔接顺？配乐贴？）
- 交付形态收尾：竖屏 `video_portrait`、字幕 `burn_subtitle`（按需）
- 交付前确认：**分镜列了几段就交了几段**，没有偷偷降级

---

## 全程红线（来自 [skill_self_decision](../skill_self_decision/SKILL.md)，违反即作弊）

| 红线 | 说明 |
|---|---|
| ❌ 不许偷偷降级 | 链式拍不出就改单段长视频凑时长 / 砍 shot 数 / `video_speed` 慢动作凑时长——全禁 |
| ❌ 不许 ask_user 甩锅 | VLM"不过审"是导演自己的判断，不许 `ask_user` 让用户 override 质量 hard stop |
| ❌ 不许脑补失败原因 | tool 报错必须把真实 error 原文带进下一步；禁止编"未配置密钥/不支持"跳过流程 |
| ✅ 唯一合法 ask_user | 主题都没说 / 预算耗尽 50% 还没出初版 / content_violation 改 2 次过不去 |

---

## 一句话记牢

**建项目 → 写分镜 → 出参考图并审 → （过 PE）逐镜拍并审 → 配音效台词 → 拼片 + 后期 BGM → 终审交付**，
其中 **参考图先行**、**调用前过 PE**、**≥2 段链式**、**每产物 VLM 审**、**BGM 走后期** 五关一个都不能少。
