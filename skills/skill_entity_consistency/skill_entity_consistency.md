# Skill · 角色 / 道具 / 场景档案库（Entity Consistency）

> **何时读我**：分镜定下来后、**任何 `gen_video_t2v` 之前**。
> **关键工具**：`entity_register` / `entity_add_view` / `entity_get`
> **目的**：保证跨镜头主体（角色/关键道具/主场景）**不变脸、不变形、不变色**。

---

## 铁律：角色档案先行

任何会在 ≥2 个镜头出现的角色 / 关键道具 / 主场景，**必须先登记 + 出参考图**，再开始拍视频。

跳过这一步 → 角色每段都换脸；老老实实建档案 → 跨镜头一致性会肉眼可见地稳。

### ⛔ 强约束：开拍门槛清单

**任何 `gen_video_t2v` 之前，必须满足全部以下条件**：

1. `storyboard.entities_planned` 里的**每一个 entity** 都已 `entity_register`（character / prop / scene 一视同仁，**prop 不许漏**）
2. 每个 entity 的 `views_needed` 列表中**所有视角全部** `entity_add_view` 出齐（不只是基准图——character 写了 `[front, side, dancing]` 就三张都要出）
3. 每张视图都已 `vlm_understand` 过审：
   - character 的 front / side / back / dancing 等**每张都审**
   - scene 的 wide / wide_dusk 等基准图也审
   - prop 的 default / in_use 等基准图也审
4. 该 shot 的 `reference_entities` 必须**列出 storyboard 中该 shot 的 characters + props + scene 的所有 entity 名**，一个都不能漏

> ❌ 反例 1（13:44 那次踩坑）：
> - storyboard 写了 prop "speaker"，但 agent 没 register 也没 add_view，s01 的 reference_entities 只有 ["dancing_cat", "living_room"]，speaker 形象失控
> - living_room.wide 出完没 vlm 直接开拍，scene 基准图过审被跳过
>
> ❌ 反例 2（13:55 那次踩坑）：
> - storyboard `golden_retriever_puppy.views_needed = [front, side, dancing]`
> - 实际只出了 front 一张就开拍，side / dancing 完全没出 → 跨镜头一致性参考图少 2 张

---

## 标准工作流

### Step 1 · 批量 `entity_register`

按 `storyboard.entities_planned` 一个一个登记（**只创建档案，不出图**）。

- `type` ∈ character / prop / scene / other
- `description` 越详越好（后续每张 view 的 prompt 都建议复用这段描述）
- `canonical_view`：character 用 `turnaround`（三视图，默认值），prop 用 `default`，scene 用 `wide`
- `views_needed` 不传时按 type 自动展开（**默认就够了，绝大多数项目用这个**）：
  - **character → `["turnaround"]`** ⭐ 1 张白底三视图（front + side + back **三个视角合在一张图里**，左→中→右排列，T-pose / 自然站姿）
  - prop → `["default"]`
  - scene → `["wide"]`

> **三视图（turnaround）= 一张图含三个视角**（行业标准 character turnaround sheet）。视频模型只参考这 1 张就够了，**Seedance / Seedream 的 reference_images 只塞这 1 张**。
>
> **何时追加额外 view**（按需，不是默认）：
> - 镜头里出现**特殊姿态/动作**（dancing / shooting / squatting / running）→ 单独 add_view 该动作
> - 角色出现 **ID 漂移**（多次重做仍换脸）→ 升级到 `face_closeup`（V-1 防 ID 漂移，专门补一张大头照）
> - 场景 / 道具有**多个状态**（dusk / broken / in_use）→ 按需加
> - **没有特殊需要就不要乱加**——1 张三视图 vs 5 张单视角图，前者已经够稳，后者徒增预算和返工概率

### Step 2 · 批量 `entity_add_view` 出图

- 第一张 = canonical_view 基准图（无 ref）
- 之后每张**自动用 canonical_view 当 ref_image_url** 强制保持一致，**不需要手动传**
- 想用别的 view 当参考时，显式传 `ref_image_url`

**character 三视图视角名固定用 `turnaround`** —— `entity_add_view` 会识别这个关键词，自动给 prompt 追加"纯白色背景、单张图含三个视角（front/side/back 左→中→右）、T-pose / 自然站姿、全身入镜、立绘风、character turnaround sheet"等素材规范。

> ⚠️ character 类型 prompt 里**只需写人物外观+服装**，不要写"在球场上奔跑"这类场景描述，否则会出成写实照片而不是参考素材。
> 例：`prompt='20岁男性篮球运动员，白色11号球衣，黑色短裤，白色运动鞋，短发'`

### Step 3 · 出 `gen_video_t2v` 时把 entity 名加进 `reference_entities`

工具会自动把每个 entity 的所有 view 一股脑塞进参考图列表。

---

## 视角/状态规划参考

| 类型 | 默认 view（auto） | 戏份重时再加 |
|---|---|---|
| character（主角） | **turnaround（1 张白底三视图，行业标准）** | face_closeup（防 ID 漂移）, action_run, action_dance |
| character（配角） | turnaround | 特写按需 |
| prop（武器/法器） | default | sheathed, drawn, in_use, broken |
| prop（杯/瓶/物件） | default | full, empty, broken |
| scene（主场景） | wide | wide_dusk, wide_night, close_corner |

> **判断标准**：剧本里出现了几种状态/景别 → 至少做几张。预留 1-2 张做"以防万一"也行；图比视频便宜得多（Seedream 5-10s/张 vs Seedance 200-300s/段）。

---

## 出图返工纪律

- 基准图（canonical_view = `turnaround`）必须 vlm 过审才能继续做其他视角
- character 的额外动作视角（face_closeup / action_run 等，**只在按需追加时存在**）也要 vlm 各审一次
- scene / prop 的基准图同样必须 vlm 过审
- 同一张图最多 2 轮返工，2 轮过不了就标记 `best_effort` 顶上
- 发现 turnaround 不完美 → 回去重做 turnaround，**不要在动作视图里"修正"**

---

## 一致性的两层保障

1. **gen → ref**：`entity_add_view` 自动用 canonical_view 当 ref → 每张视图都延续基准造型
2. **ref → 视频**：`gen_video_t2v` 把 entity 名加进 `reference_entities` → 工具自动把所有 view 喂给 Seedance

跳过这两层 → 直接 `gen_video_t2v` 写"穿月白长袍的书生"——每段都是不同的脸，凑不成一部片子。

---

## entity_get 的典型用法

- 不传 `entity_name` → 列出全部 entity 摘要（type / description / 已有 views）
- 传了 → 返回完整 ref.json，含每个 view 的 file 和 url
- 出 `gen_video_t2v` 之前要单独抽某张概念图加进 `reference_images` 时用；一般直接传 entity 名进 `reference_entities` 更省事

---

## V-1 ⭐ 防 ID 漂移（character 必读）

**典型现象**：生成的角色与参考图不一致；视频中途**换脸**；甚至撞脸明星被审核拦截。

**官方根因**：人脸参考图的有效性不足——
- 把人脸图与全身/半身/服装/细节图**混在同一张图里**给模型，人脸区域占比太小，模型权重不够
- 用三视图当人脸参考——脸不够大、不够清晰

### 官方解法（三视图 + 大头照分开）

| 旧 views_needed | 升级版 views_needed（**仅当出现 ID 漂移再用**） |
|---|---|
| `["turnaround"]` | `["turnaround", "face_closeup"]` |

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
    reference_entities=["protagonist_li"],   # 自动展开 turnaround / face_closeup
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
# ❌ 错误：直接 8 张 entity 单人图
gen_video_t2v(
    reference_entities=["c1","c2","c3","c4","c5","c6","c7","c8"],
    ...
)

# ✅ 正确：先合成 2 张分组图
gen_image(prompt="角色1+角色2+角色3+角色4 站在<场景>", name="group1")
gen_image(prompt="角色5+角色6+角色7+角色8 站在<场景>", name="group2")
gen_video_t2v(
    prompt="[图1]和[图2]全景，高角度俯拍...",
    reference_images=["projects/<pid>/shots/group1.png",
                      "projects/<pid>/shots/group2.png"],
    ...
)
```

**判定**：storyboard 里**任何一个 shot 的 characters + props 总数 > 4** 时触发本条，自动改用"分组图法"。

---

## 基准图过审后造型锁死（来自 director_vlm 反例）

**典型踩坑**：turnaround 已过审且实际是"双足拟人小马"，agent 在生成 face_closeup 时强加 prompt"四足卡通可爱小马，四足肢体结构正确" → 跟工具自动注入的 turnaround ref_image_url 打架 → 模型混乱 → 不过审 → 重做又不过 → ask_user 兜底。

**根因**：基准图 vlm 过审 = **造型基准已确立**。后续视图必须**继承基准的所有特征**，**不许加新约束**。

**铁律**：

- turnaround 过审 → 后续 face_closeup / action_xxx 的 prompt **只描述视角差异/动作差异**，**不改造型**
- 觉得 turnaround 不对 → **回去重做 turnaround**，不要在 face_closeup 里"修正"
- face_closeup 过审标准是"**面部对了 + 与 turnaround 同一个角色**"，不是"再次审一遍造型"
