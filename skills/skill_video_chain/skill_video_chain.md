# Skill · 视频链式衔接（Video Chain）—— 这条片子是不是会断掉，全看它

> **何时读我**：要调 `gen_video_t2v` 之前。**只要项目里 ≥2 段视频，必读**。
> **关键工具**：`gen_video_t2v` / `query_video_task` / `video_concat` / `video_crossfade` / `video_trim`
> **配套**：音频连贯性详见 [skill_audio.md](../skill_audio/skill_audio.md)（reference_audios 共享 / MV 模式 / 4 类特殊字符）

---

## 头号铁律 ⭐

> **只要项目里有 ≥2 段 Seedance 视频，从第 2 段开始的 `gen_video_t2v` 必须传 `reference_video_url=上一段视频的 url`。**

无论你给这两段起什么名字（`shot_s01` + `shot_s02`、`part1` + `part2`、`scene_a` + `scene_b`），**只要它们在最终成片里前后相邻**，第 2 段就必须参考第 1 段。

这一条违反 = 主体跳变 + 动作断裂 + 配乐违和（典型崩坏 case 见文末）。

**唯一例外**：刻意做"快剪/蒙太奇"风格、或用户明确要求"独立片段拼接"。

---

## 唯一的视频生成路径 · 多模态参考模式

`gen_video_t2v` 只支持一条路径：

- 入参 = `prompt` + `reference_entities` + `reference_images` + `reference_video_url`
- 上限：最多 9 张参考图 + 1 段参考视频 + 文字 prompt
- **没有 first_frame / last_frame / image_url 这种东西**——首尾帧靠 prompt 文字暗示（`opening frame: ...; ending frame: ...`）

这是官方做精良商业短剧的标准姿势（水果茶广告 demo 11 秒长镜头就是 2 图 + 1 视频 + 1 音频做出来的）。

---

## 标准姿势

### 第 1 段（开篇）

```python
gen_video_t2v(
    prompt="<本镜动作 + 镜头语言 + 光影氛围>",
    reference_entities=[本镜出现的所有 entity 名],   # 角色三视图、道具各状态、场景各景别都打包
    reference_images=[关键概念图 url],               # 可选，1-2 张为宜
    duration=6,
    name="shot_s01",
)
```

### 第 2 段及之后（链式段）

```python
# 上一段产物来自：q = query_video_task(task_id=..., save_name="shot_s01")
# q 返回 {path, video_url, ...}—— 两个都可以传给 reference_video_url
gen_video_t2v(
    prompt="承接上段视频，<角色名> 接着做 ... 动作。<本镜的镜头语言/光影>",
    reference_entities=[本镜出现的所有 entity 名],
    reference_video_url=q["video_url"],               # ⭐ 必传（也可传 q["path"]）
    duration=6,
    name="shot_s02",
)
```

**prompt 里必须显式写"承接上段视频"** —— 这是给模型的明确信号，它会主动延续上段结尾的动作/姿态/光影。

### reference_video_url 入参格式（重要）

工具支持两种格式，**任选其一**：

| 入参 | 行为 |
|---|---|
| `https://...` 云端 url（来自 `query_video_task` 的 `video_url` 字段） | 直接传给 Seedance |
| 本地 path（如 `shots/shot_s01.mp4`） | 工具自动反查同名 sidecar `<path>.url.txt` 拿云端 url |
| 既不是 url、也没找到 sidecar | 工具抛错，提示你先跑 `query_video_task` 把视频落盘 |

> 老姿势踩过的坑：直接把本地 path 当成 url 拼到 HTTP body 里 → Seedance 返回 `400 invalid url`。**现在工具兜底了**，但仍然推荐传 `q["video_url"]`，最直接。

---

## 参考媒体配比建议

reference_images 上限 9 张。entity 默认三视图 = 1 张（turnaround，含 front/side/back 合一）。第 N 段（N≥2）的标准配置：

| 项目 | 数量 |
|---|---|
| 本镜 entity 的所有 view（reference_entities 自动展开） | 1-4 张（character × 1 turnaround + props/scene 各 1） |
| 关键概念图（reference_images，可选） | 0-2 张 |
| **上一段视频**（reference_video_url） | **1 段，必传** |

不要塞太多反而稀释信号。

---

## 调度铁律：链式必串行

- 第 N 段（N≥2）需要上一段视频的 url，**必须等上一段 `query_video_task` 拿到 mp4** 之后再 submit
- 而且**上一段不过 vlm 检查不许提交下一段**——崩坏会累积传染
- 非链式片段（无衔接关系）才能并行 submit + 依次 query

详细调度策略见 [skill_async_schedule.md](../skill_async_schedule/skill_async_schedule.md)。

---

## 配乐策略（搭配链式衔接的关键）

> **完整音频规范见 [skill_audio.md](../skill_audio/skill_audio.md)。** 这里只列项目级铁律。

### 项目级铁律 ⭐

> **Seedance 只出"对话 + 音效"，BGM 一律走后期 `audio_amix` 整片铺底。**

为什么：Seedance 单段 BGM 各段调性/节奏/响度不一，**怎么拼都会断**；后期一次性铺底 → 整片就一首 BGM、一种响度，**根本不存在"段间断裂"问题**。

### 落地三步

1. **每段生成**：`generate_audio=True`；prompt 用 `<音效> {对白}`，**禁用 `（BGM）`**；从第 2 段起 `reference_audios=[第1段video_url]` 让对白音色连贯
2. **拼接**：先做 V-6 帧裁剪（前段-6 帧 / 后段-1 帧），再 `video_concat` 出干净底片
3. **铺 BGM**：`audio_amix(base_video=干净底片, bgm_audio=外部BGM, bgm_volume=0.15~0.25)`

### ❌ 反例

- 每段 prompt 写 `（轻快电子乐）` → Seedance 各段 BGM 节拍/响度不一致 → concat 必断
- 每段 `generate_audio=True` 出原生 BGM 后直接 `video_concat` → 必断
- BGM 来源用 `gen_bgm`（当前禁用）→ 强行调用会报错

```python
# ✅ 标准代码
shot1 = query_video_task(q1["task_id"], save_name="shot_s01", duration=8)

# 第 2 段：对白音色承接，但 prompt 不带 BGM
q2 = gen_video_t2v(
    prompt="承接上段视频，<球网响声> {好球！}",   # 只有音效+对白
    name="shot_s02",
    reference_video_url=shot1["video_url"],
    reference_audios=[shot1["video_url"]],   # 对白音色连贯，不为 BGM
    generate_audio=True,
    duration=6,
)

# ... 所有段 query 落盘后

# V-6 帧裁剪 + concat
video_concat([trimmed_segments...], "final_no_bgm")

# 整片一次性铺 BGM
audio_amix("final_no_bgm.mp4", "external_bgm.mp3", "final_with_bgm", bgm_volume=0.18)
```

---

## 链式片子的 vlm 必查项

链式段视频出来后，`vlm_understand` 必须问：

- "这一段的开头画面，是否自然承接了上一段的结尾？人物/场景/动作是否一致？"
- "和上一段对比，主体（人物、车辆、关键道具）的造型/颜色/视角是否保持一致？"
- "本段角色对白音色 / 现场音效与上一段是否一致？结尾有没有'咔哒'截断音？"
- ⚠️ **不要问 BGM**——本项目 Seedance 不出 BGM，BGM 走后期 `audio_amix`

发现衔接断裂 → 重做这一段（reference_video_url 仍指上一段视频，prompt 强调"承接"）。

---

## 反面教材（不要再犯）

### Case A · 30s 小猫跳舞，4 段 → 实际拍了 1 段慢动作（13:06 那一版）

- ❌ 实际做法：s01 拍完后想拍 s02，把**本地 path** 当 reference_video_url 喂给工具 → Seedance HTTP 400
  - 失败后 agent 直接放弃 4 段链式，改成单段 `gen_video_t2v(duration=15)` + `video_speed(factor=0.5)` 慢动作凑 30s
- 后果：分镜形同虚设、镜头单调、慢动作画面发糊
- ✅ 正确做法：
  - 链式段把 `query_video_task` 返回的 `video_url`（或 `path`，工具会反查 sidecar）传给 `reference_video_url`
  - 失败时**先诊断不要降级**：读错误 → 排查参数（详见 [skill_self_decision.md](../skill_self_decision/skill_self_decision.md) "失败不许降级"）
  - 严禁用 `video_speed` 慢动作凑时长——分镜规定几段就拍几段

### Case B · 30s 小猫跳舞 part1+part2（早期版本）

- ❌ 实际做法：part1 和 part2 都 `reference_video_url=null`，各喂一份 entity turnaround
- 后果：part2 开头橘猫姿态/位置/光影完全不接 part1 结尾；两段原生音节拍不统一硬拼违和
- ✅ 正确做法：part2 必须传上段 url；**Seedance 只出对白+音效不出 BGM**，整片拼好后再统一 `audio_amix` 铺一首 BGM（详见 [skill_audio.md](../skill_audio/skill_audio.md)）

---

## V-6 ⭐ 拼接帧裁剪铁律（官方解法）

**典型现象**：用"延长模式 / 链式段"按顺序生成的视频，使用 `video_concat` 直接拼接后，在衔接处（第 5s / 10s / 15s 等切换点）出现**画面瞬间跳动 / 内容回退**——肉眼可见的"咔"。

**官方根因**：Seedance 在续写每段时，会在**首尾各产生轻微残影/回退帧**——前段尾部最后几帧抖、后段头部第 1 帧重复。

**官方裁剪量（写死，不许改）**：

| 位置 | 裁剪量 |
|---|---|
| 前段视频**末尾** | 删 **6 帧** |
| 后段视频**开头** | 删 **1 帧** |

**24 fps 下换算**：6 帧 ≈ 0.250s；1 帧 ≈ 0.042s

### 标准操作（链式段拼接前必做）

```python
# 假设 shot_s01 / shot_s02 / shot_s03 都已 query_video_task 落盘
# 在 video_concat 之前，对每个衔接点做帧对齐：

# 探测原段时长
s01_dur = ...   # ffprobe 拿到
s02_dur = ...

# 前段尾去 6 帧（24fps 下约 0.25s）
trim_back  = lambda d: d - 6 / 24

# 后段头去 1 帧（24fps 下约 0.042s）
trim_front = lambda: 1 / 24

video_trim("shots/shot_s01.mp4", 0,                trim_back(s01_dur),  "s01_trimmed")
video_trim("shots/shot_s02.mp4", trim_front(),     trim_back(s02_dur),  "s02_trimmed")
video_trim("shots/shot_s03.mp4", trim_front(),     s03_dur,             "s03_trimmed")  # 末段不用裁尾

video_concat(["s01_trimmed.mp4", "s02_trimmed.mp4", "s03_trimmed.mp4"], "final")
```

**裁剪后仍有细微跳变**：在续写生成时把"上一段以转场切镜结尾、下一段以新场景开始"作为 prompt 约束，让模型自己规避瞬态衔接。

> ⚠️ 这一条是字节官方文档明确给出的**唯一正解**——不要再去发明 acrossfade、xfade、blend 这些路子，它们解决不了"内容跳变/回退"，只能解决"音频硬切"。

---

## V-8 · 视频延长画质劣化（链式 ≥3 段必看）

**典型现象**：将上一段生成的视频作为 `reference_video_url` 续写下一段时，**画质会劣化**；多次续写会**叠加劣化**——人脸出现斑驳色块、背景变糊。

**官方解法（链式 ≥3 段时必用）**：

1. 把上一段视频先用 Seedance 转成**白模视频**：
   ```python
   gen_video_t2v(
       prompt="将视频修改成白色 3D 模型，所有人物都是白色3D模型，"
              "无颜色，无纹理，无阴影，纯白背景，结构稳定，运动流畅。",
       reference_video_url=shot_prev["video_url"],
       name="shot_prev_whitemodel",
       generate_audio=False,
   )
   ```
2. 用**白模视频**作为下一段的 `reference_video_url`
3. 同时把**高清的人物图（entity views） + 高清场景图**继续放进 `reference_entities`
4. 模型用白模学**结构 / 运动 / 镜头**，用 entity 高清图学**外观 / 颜色 / 纹理** → 输出画质重新对齐基准，不再劣化

> 适用门槛：单链段数 ≥3。1-2 段链式不需要白模化（劣化不明显）。
> 代价：每个白模化步骤多烧 1 次 Seedance 调用，预算要相应预留。

