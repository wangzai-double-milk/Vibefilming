---
name: skill-entity-consistency
description: 分镜定稿后、任何 gen_video_t2v 之前；要保证角色/道具/场景跨镜头一致、防变脸/防双胞胎/多人场景时。 用 gen_image 出参考图、再把图的本地 path 放进 gen_video_t2v 的 reference_images，保证跨镜头不变脸、不变形、不变色。
---

# Skill · 角色 / 道具 / 场景一致性（Entity Consistency）

> **何时读我**：分镜定下来后、**任何 `gen_video_t2v` 之前**。
> **关键工具**：`gen_image`（出参考图）+ `gen_video_t2v` 的 `reference_images`（喂参考图）+ `vlm_understand`（审参考图）。
> **目的**：保证跨镜头主体（角色/关键道具/主场景）**不变脸、不变形、不变色**。

> ⚠️ 没有"entity 档案库"这种工具了。一个角色/道具/场景**就是几张参考图**——你用 `gen_image` 生成它，记住返回的**本地 `path`**，出视频时把 path 放进 `reference_images`（工具会自动 base64 内嵌）。**优先传 path，别去转抄那条很长的带签名 `url`**——实测转抄长 url 时极易把末尾 `?X-Tos-Signature=...` 签名段漏掉，丢给 Seedance 一个无签名裸 url，服务端就报"resource download failed"。path 很短、转抄不会出错。所谓"档案"就是你自己在上下文里记住的「主体名 → 图 path」对应关系（必要时 `file_write` 一个小 json 落盘备查）。但是注册好的东西得放在 entity 文件夹里。

> 📁 **参考图落盘规范（强制）**：所有参考图（角色三视图 / 大头照 / 场景图 / 道具图）的 `gen_image` `name` **必须以 `ref_` 开头**——工具据此自动落到 `entities/` 目录，跟 `shots/`（关键帧 + 镜头视频）分开。命名格式 `ref_<主体>_<view>`，如 `ref_dancer_girl_front` / `ref_living_room_wide` / `ref_sword_default`。**不带 `ref_` 前缀的图会落进 shots/ 跟视频混在一起**，是错的。

---

## 铁律：参考图先行

任何会在 ≥2 个镜头出现的角色 / 关键道具 / 主场景，**必须先用 `gen_image` 出参考图、`vlm_understand` 过审**，再开始拍视频。

跳过这一步 → 角色每段都换脸；老老实实出参考图 → 跨镜头一致性会肉眼可见地稳。

### ⛔ 开拍门槛清单（任何 `gen_video_t2v` 之前必须满足）

1. 分镜里出现的**每一个主体**（character / prop / scene，**prop 不许漏**）都已用 `gen_image` 出好参考图
2. 每张参考图都已 `vlm_understand` 过审（character 基准图、scene 的 wide、prop 的 default 都要审）
3. 该 shot 的 `reference_images` 必须**列全本 shot 出现的所有角色 + 道具 + 场景的参考图（传本地 path）**，一个都不能漏

> ❌ 反例 1：分镜写了道具 "speaker"，但 agent 没出它的参考图，s01 的 reference_images 只放了猫和客厅的图 → speaker 形象每段乱变。
> ❌ 反例 2：客厅场景图出完没 vlm 审就直接开拍 → 场景基准没确认，后面镜头穿帮。

---

## 标准工作流

### Step 1 · 规划要出哪些参考图

对照分镜，列出所有跨镜头主体，按下表决定每个主体出几张图（**默认每个主体 1 张就够**）：

| 类型 | 默认参考图 | 戏份重时再加 |
|---|---|---|
| character（主角） | **1 张白底三视图（turnaround）** | face_closeup（防 ID 漂移）, action_run, action_dance |
| character（配角） | 1 张三视图 | 特写按需 |
| prop（武器/法器） | 1 张 default | sheathed, drawn, in_use, broken |
| prop（杯/瓶/物件） | 1 张 default | full, empty, broken |
| scene（主场景） | 1 张 wide | wide_dusk, wide_night, close_corner |

> **三视图（turnaround）= 一张图含 front+side+back 三个视角**（行业标准 character turnaround sheet）。视频模型参考这 1 张就够，**别为一个角色出一堆单视角图**——1 张三视图比 5 张单视角更稳，还省预算。
> **何时追加额外图**（按需，不是默认）：特殊姿态/动作（dancing/shooting）单独出一张；出现 ID 漂移升级 face_closeup；场景/道具有多状态（dusk/broken）按需加。图比视频便宜得多（Seedream 5-10s/张 vs Seedance 200-300s/段），但没需要别乱加。

文件命名建议：`ref_<主体名>_<view>`（**必须 `ref_` 前缀**才会落进 entities/），如 `ref_protagonist_li_turnaround` / `ref_living_room_wide` / `ref_sword_default`，方便你自己记 url。

### Step 2 · 用 `gen_image` 逐张出图（基准图 → 锁参考续作）

`gen_image` 是**原子工具**：prompt 原样发给 Seedream，`ref_image_url` 传不传由你决定。**素材规范要你自己写进 prompt**；**续作图要你自己把基准图 path 传进 `ref_image_url` 锁一致性**。

- **基准图**（每个主体第一张）：`ref_image_url` 留空（无参考）。出完记住返回的**本地 `path`**。
- **续作图**（同主体的其他视角/动作/状态）：把基准图的**本地 `path`** 显式传进 `ref_image_url`，强制延续基准造型。
- **turnaround 三视图用宽尺寸**：`size="1920x1080"`，否则三视图挤一张方图里排不开。
- 每出一张基准图，立刻 `vlm_understand` 审，过审再出续作。

#### prompt 模板（直接复制，按需替换描述）

**① turnaround 三视图**（character 基准图，`size="1920x1080"`，`ref_image_url` 留空）：
```
<人物外观+服装，如：20岁男性篮球运动员，白色11号球衣，黑色短裤，白色运动鞋，短发>

【素材规范 · character turnaround sheet · 严格遵守】一张图，**同一个角色的三视图**：左侧正面 front view、中间侧面 side view（朝画面右侧 90 度）、右侧背面 back view，三个视角左→中→右水平排列，**全部为同一角色**，造型/服装/发型/配饰完全一致；纯白色背景，无任何环境元素、无影子、无杂物；每个视角下角色全身入镜（头到脚），T-pose 或自然站姿，双手自然下垂或微微张开；光线均匀、无明显阴影；游戏角色立绘 / 概念设计稿 / character reference sheet 风格。
```

**② face_closeup 面部特写**（防 ID 漂移，`ref_image_url` 传 turnaround 的 url）：
```
<同一角色的面部描述>

【素材规范 · 面部特写大头照 · 严格遵守】仅面部特写，精确裁剪到下颌底部以上，几乎不留颈部、肩部、背景；正面、表情中性、双眼直视镜头；纯白色背景；光线均匀、无明显阴影；高清人脸参考素材，用于视频模型 ID 锁定。
```

**③ 单视角立绘 / prop / scene**（按需）：
```
<外观描述>

【素材规范，严格遵守】纯白色背景，无任何环境元素、无影子、无杂物；人物全身入镜（头到脚），居中构图；<正面 front view / 背面 back view / 侧面 side view（身体朝画面左或右 90 度）/ 3-4 侧前方视角 等，按需替换>；自然站姿，双手自然下垂或微微张开；光线均匀，无明显阴影；角色概念图风格，类似游戏角色立绘素材。
```

> ⚠️ character 类型 prompt 里**只需写人物外观+服装**，不要写"在球场上奔跑"这类场景描述，否则会出成写实照片而不是参考素材。
> 例：`prompt='20岁男性篮球运动员，白色11号球衣，黑色短裤，白色运动鞋，短发\n\n【素材规范...】'`

### Step 3 · 出 `gen_video_t2v` 时把参考图 url 放进 `reference_images`

把本 shot 出现的所有主体的参考图 url 收集进 `reference_images`（最多 9 张）：

```python
gen_video_t2v(
    prompt="主体1@图片1（三视图妆造）旋身开合折扇，背景是@图片2（客厅）...",
    reference_images=[
        "<protagonist_li_turnaround 的 url>",
        "<living_room_wide 的 url>",
    ],
    name="shot_s01",
)
```

---

## 出图返工纪律

- 基准图（turnaround / wide / default）必须 `vlm_understand` 过审才能继续做续作图或开拍
- character 的额外动作视角（face_closeup / action_run 等，**只在按需追加时存在**）也要 vlm 各审一次
- 同一张图最多 2 轮返工，2 轮过不了就标记 `best_effort` 顶上
- 发现 turnaround 不完美 → 回去重做 turnaround，**不要在续作图里"修正"**

---

## 一致性的两层保障

1. **gen → ref**：每张续作图都把基准图 url 显式传进 `gen_image` 的 `ref_image_url` → 延续基准造型
2. **ref → 视频**：`gen_video_t2v` 的 `reference_images` 放全本 shot 主体的参考图 url → 喂给 Seedance

跳过这两层 → 直接 `gen_video_t2v` 写"穿月白长袍的书生"——每段都是不同的脸，凑不成一部片子。

---

## V-1 ⭐ 防 ID 漂移（character 必读）

**典型现象**：生成的角色与参考图不一致；视频中途**换脸**；甚至撞脸明星被审核拦截。

**官方根因**：人脸参考图的有效性不足——
- 把人脸图与全身/半身/服装/细节图**混在同一张图里**给模型，人脸区域占比太小，模型权重不够
- 用三视图当人脸参考——脸不够大、不够清晰

### 官方解法（三视图 + 大头照分开）

**仅当出现 ID 漂移再用**：给角色额外出一张 `face_closeup` 大头照（`gen_image`，ref 传 turnaround 的 url），出视频时三视图 + 大头照两张都放进 `reference_images`。

**`face_closeup` 必须满足**：
- **只包含面部**（精确裁剪，几乎不留颈部、肩部、背景）
- 正面、表情中性、光线均匀
- 单独作为一张参考图存在，**不要和三视图合并**

> 默认情况下不要主动加 `face_closeup`——只在视频出现 ID 漂移、连续 2 轮重做仍换脸时才升级。多一张图多一份预算和返工。

### 在 prompt 里显式分配引用

```
主体1的面部特征参考@图片2（大头照），妆造和姿态参考@图片1（三视图）
```

工具调用时：

```python
gen_video_t2v(
    prompt="主体1@图片1（三视图妆造）@图片2（面部）旋身开合折扇...",
    reference_images=["<turnaround url>", "<face_closeup url>"],
    name="shot_s01",
)
```

### 重要的素材放在 prompt 前部

> 越需要精准参考的素材，放在越前面。人脸 > 妆造 > 场景 > 道具。

### 反例

- ❌ 只用三视图 + 出现 ID 漂移仍不补 face_closeup → 撞脸明星
- ❌ 把脸+全身+细节塞同一张图 → 脸部权重被稀释
- ✅ 三视图（妆造姿态） + 大头照（面部）分开两张，prompt 写清楚谁参考谁

---

## V-7 · 防双胞胎（同一画面里别复制人）

**典型现象**：画面中出现 2 个几乎一模一样的人物主体（在多人场景 + 三视图参考下尤其常见）。

**官方解法（4 条全做）**：

1. **明确人物-参考图对应关系**：每次提及人物都写 `人物名（对应图片N）`，全程格式一致
2. **prompt 末尾加禁忌指令**（直接复制粘贴）：
   ```
   视频全程禁止出现外形、着装、配饰完全相同的两个人，
   杜绝双胞胎同款人物，保证画面中同一时刻始终只有一个某人物，
   不出现重复人物、分身或双胞胎效果。
   ```
3. **不建议用多视图作为人物参考图片**（同 V-1）
4. **精简提示词**：剧本不可直接当 prompt，内容过冗余会让模型混乱

> 适用情景：一个 shot 里 ≥2 个角色 + 角色长得有点像 / 都用三视图参考。

---

## V-10 · 参考人物超过 4 人的处理（多人场景必读）

**官方限制**：当**参考人物超过 4 人**时，模型表现不稳定——可能少人、多人、出现重复人物。

**官方解法（先生图再生视频）**：

1. **第一步：分批生成图片**——把人物分组，每张图人物数 **≤4 人**
   - 8 个角色 → 生 2 张图，每张 4 人
   - 6 个角色 → 生 2 张图，每张 3 人
2. **第二步：用这些"分组图"作为 reference_images 生视频**——而不是直接喂 8 张单人图

```python
# ❌ 错误：直接喂 8 张单人图
gen_video_t2v(reference_images=[c1, c2, c3, c4, c5, c6, c7, c8], ...)

# ✅ 正确：先合成 2 张分组图
gen_image(prompt="角色1+角色2+角色3+角色4 站在<场景>", name="group1")
gen_image(prompt="角色5+角色6+角色7+角色8 站在<场景>", name="group2")
gen_video_t2v(
    prompt="[图1]和[图2]全景，高角度俯拍...",
    reference_images=["<group1 url>", "<group2 url>"],
    ...
)
```

**判定**：分镜里**任何一个 shot 的角色 + 道具总数 > 4** 时触发本条，自动改用"分组图法"。

---

## 基准图过审后造型锁死（来自 director_vlm 反例）

**典型踩坑**：turnaround 已过审且实际是"双足拟人小马"，agent 在出 face_closeup 时强加 prompt"四足卡通可爱小马，四足肢体结构正确" → 跟 ref_image_url 传的 turnaround 打架 → 模型混乱 → 不过审 → 重做又不过 → ask_user 兜底。

**根因**：基准图 vlm 过审 = **造型基准已确立**。后续续作图必须**继承基准的所有特征**，**不许加新约束**。

**铁律**：

- turnaround 过审 → 后续 face_closeup / action_xxx 的 prompt **只描述视角差异/动作差异**，**不改造型**
- 觉得 turnaround 不对 → **回去重做 turnaround**，不要在续作图里"修正"
- face_closeup 过审标准是"**面部对了 + 与 turnaround 同一个角色**"，不是"再次审一遍造型"
