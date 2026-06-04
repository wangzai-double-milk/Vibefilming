---
name: skill-storyboard
description: 拿到 brief、要把需求拆成多镜头分镜时（project_create 之后第一件事）。 分镜=交付契约：先用 file_write 把每个 shot 的镜头/主体/时长/音频意图写到 projects/<pid>/storyboard.json，后续所有产出都对着分镜审。
---

# Skill · 分镜（Storyboard）—— 影视开机的第一件事

> **何时读我**：拿到 brief，做完 `project_create` 之后。
> **存盘方式**：用通用 `file_write` 写 / `file_read` 读 `projects/<pid>/storyboard.json`（覆盖式，可多次改）

---

## 铁律：分镜先行

`project_create` 之后**第一件事不是出图**，是用 `file_write` 把分镜 JSON 落盘到 `projects/<pid>/storyboard.json`。
- 单镜头视频可省，**≥2 镜头必做**
- 没分镜直接拍 → 主体漏出参考图 / 镜头堆砌不连贯 / 预算失控（前几镜成废片）

## 分镜必含字段

整体：`title / duration_total / ratio / style / synopsis / entities_planned[] / shots[]`

每个 shot：`id / duration / scene / characters / props / camera / action / audio / chain_with`

参考结构：

```json
{
  "title": "月下抚琴",
  "duration_total": 30,
  "ratio": "16:9",
  "style": "唐风武侠，水墨质感",
  "synopsis": "书生月下抚琴，遇剑客寻仇，琴音化剑气退敌",
  "entities_planned": [
    {"name": "protagonist_li", "type": "character",
     "description": "20岁书生，月白长袍，剑眉星目",
     "views_needed": ["turnaround"]},   // 计划用 gen_image 出 1 张三视图（front/side/back 合一）
    {"name": "guqin", "type": "prop",
     "description": "焦尾古琴，桐木琴身",
     "views_needed": ["idle","being_played"]},
    {"name": "courtyard", "type": "scene",
     "description": "月夜庭院，竹影摇曳",
     "views_needed": ["wide_night","close_corner"]}
  ],
  "shots": [
    {"id":"s01","duration":6,"scene":"courtyard","characters":["protagonist_li"],
     "props":["guqin"],"camera":"中景缓推","action":"李慕白盘膝抚琴",
     "audio":false,"chain_with":null},
    {"id":"s02","duration":5,"scene":"courtyard","characters":["protagonist_li","swordsman"],
     "camera":"切到剑客全身","action":"剑客现身屋顶，剑指李慕白",
     "audio":false,"chain_with":"s01"}
  ]
}
```

---

## 写完必做：自审 5 条（self_review）

**写在思考的 `<self_review>` 块里**，不通过就再次 `file_write` 覆盖更新 `storyboard.json`（最多 2 轮，第 3 轮直接走下去）。

| # | 标准 | 怎么查 |
|---|---|---|
| 1 | 节奏配比 | 单镜 5-8s（最长 15s）；总时长按 brief；开头/高潮/收尾节奏对吗？ |
| 2 | 衔接合理性 | `chain_with` 链下来动作连得上吗？有没有突然切到无关画面？ |
| 3 | 主体完整性 | shots 里出现的所有 character/prop/scene 都在 `entities_planned` 里吗？ |
| 4 | 镜头多样性 | camera 全是"中景静态"？混入推/拉/特写/俯拍/横移 |
| 5 | 预算估算 | `len(shots) × 1.2 重做率` 不超过 `max_seedance_calls` 才安全 |
| **6** | **entities_planned 非空** ⭐ | **任何真实视频项目（不是抽象概念图测试）都必须列出至少 1 个主体。空列表直接打回重写**——已踩坑：p20260531_140047_ 环保广告 entities_planned 为空，agent 跳过出参考图流程靠文字脑补人物，第一次出年轻人不符合"中年男子" |

---

## 读分镜的典型用法（file_read storyboard.json）

- 出每个 shot 之前先 `file_read` 一次，看 action / camera / characters
- vlm 审片时从这里取 `synopsis` 当"创作诉求"
- 写承接 prompt 时从这里读上一镜的 action

---

## 跳过分镜的代价（提醒自己别偷懒）

- 主体漏出参考图 → 拍到一半发现某角色没出过参考图，回头补 → 风格会跳
- 镜头堆砌不连贯 → vlm 审片时全是"承接断裂"，返工成本远超提前规划
- 预算失控 → 第 5 镜超预算被迫停手，前 4 镜成废片

---

## 分镜 = 交付契约（不许偷偷砍）

`storyboard.shots` 一旦落盘，就是**对用户的承诺**：

- shots 列了 4 段就要拍 4 段，列了 6 段就要拍 6 段
- ❌ **严禁** "拍 1 段长视频 + `video_speed(0.5x)` 慢动作 / `video_trim` 切多段" 来假装满足分镜
- ❌ **严禁** 拍着拍着发现某段难拍，悄悄从 4 段砍成 2 段然后交付
- ✅ 真有不可抗（API 报错、内容违规反复改不过）→ 必须 `ask_user` 显式告知 "因 X 原因无法完成第 N 段，是否接受 M 段或换方向"

详见 [skill_self_decision.md](../skill_self_decision/SKILL.md) "失败处置：不许偷偷降级"。

---

## 任务类型（写 prompt 必先选一类）

Seedance 2.0 把 R2V 任务分成 3 类，**句式不同模型行为不同，混用会出错**：

| 类型 | 何时用 | 推荐句式 |
|---|---|---|
| **参考（Reference）** | 从素材中提取元素（主体/风格/场景/音效）生成全新视频 | `参考<图片N>中的<主体N>，生成...`<br>`参考<视频N>中的[动作/运镜/风格/音效]，生成...`<br>`参考<音频N>中的[音色]，生成...` |
| **编辑（Edit）** | 在原视频基础上做局部/全局修改 | `严格编辑<视频N>，将其中的[原特征]修改为[新特征]...` |
| **延长（Extend）** | 在时间维度上延续原视频 | `延长<视频N>，生成...` / `向前延长<视频N>，生成...` |

> ⚠️ **编辑 / 延长** 任务里**禁止**写"参考视频N"，直接写`<视频N>`。否则会被识别成"参考"任务，结果完全不同。

**组合任务**（参考一个 + 编辑另一个）：
> `参考<图片/视频N>的[参考维度]，严格编辑<视频X>，[具体编辑内容]`

---

## 主体定义（多角色场景必写）

凡引用素材里的特定对象（人/道具/场景），**必须明确定义主体**，否则模型会指代混乱。

### 单素材单主体

```
将<图片1>中"穿红色连衣裙、戴草帽的女人"定义为<主体1>
```

**核心特征要求**：用 2-3 个**清晰、稳定的静态特征**（服饰/发型/外观/类别），保证唯一可识别。
不要写"开心地笑着"这种情绪/动作特征——后续可能不出现就指代失败。

### 多素材同一主体

```
将<图片1>中的[红裙草帽女人]、<图片2>中的[红裙草帽女人] 定义为<主体1>
```

### 多主体场景（带稳定标签区分）

```
将<视频1>中的高个子男人定义为<主体1>(警察)
将另一个矮个子男人定义为<主体2>(小偷)
```

> ⚠️ Asset 库的素材在 prompt 里**仍然要用图1/视频1指代**——模型不会把 Asset ID 直接对应上，写 Asset ID 会失败。
> ⚠️ 后续描述每次涉及主体时**必须明确指代**（每出现一次主体就提一次 `<主体1>` 或人物名），不要省略。
> ⚠️ 描述简洁，避免冗余、避免语义冲突（同一主体不要出现矛盾特征）。
> ⚠️ 空间关系优先靠**参考图**表达，少用复杂文字描述。

---

## 动态描述顺序（必须按此顺序）

prompt 里描述画面变化时，按 **镜头 → 主体 → 空间 → 音频** 的顺序：

1. **运镜或镜头切换**（中景缓推、快切、特写、拉镜...）
2. **主体动作与表情**（细化到肢体：手/腿/头部 + 程度：缓慢抬手、快速转头、用力蹬地）
3. **位置或空间变化**
4. **音频信息**（用 4 类特殊字符，详见下文）

### 时序表达

- ✅ 支持镜头顺序（镜头1、镜头2、镜头3）
- ❌ 对精确秒数（如 0-3 秒）支持不稳定 → **不要写"3 秒后..."**
- 优先让模型按剧情自然生成节奏，强制时长会导致结果不稳定

### 语言规范

- 台词语言**必须统一**，避免中英混用（专有名词除外）
- 同一段视频里说中文就全中文，说英文就全英文

---

## 特殊字符规范（4 类括号必须用对）

Seedance 靠括号识别**音乐 / 音效 / 台词 / 字幕** 4 种音频意图——不写括号 = 模型自己猜：

| 内容 | 符号 | 例子 | 本项目用法 |
|---|---|---|---|
| **音乐 / BGM** | `（）` | `（背景中播放着快节奏的摇滚乐）` | 🚫 **本项目禁用**——BGM 走后期 `audio_amix` |
| **音效** | `<>` | `<远处传来狗叫声>` | ✅ 主力，丰富现场感 |
| **台词** | `{}` | `{你好，世界}`；小语种需标注：`用日语说道{こんにちは}` | ✅ 主力，对白必用 |
| **字幕** | `【】` | `【第一章：启程】` | ⚠️ 一般禁用（V-2 字幕乱出） |

> ⭐ **项目级铁律**：Seedance 只生成"对话 + 音效"，**BGM 一律走后期 `audio_amix` 整片铺底**。
> prompt 里禁止使用 `（）` 标注 BGM，遇到 BGM 描述一律删除或改写为 `<环境音效>`。
> 详细规范见 [skill_audio.md](../skill_audio/SKILL.md)。

### 时长 / 比例参数双保险

接口已经传 duration / ratio，但**官方建议在 prompt 最前/最后再复述**，且**用中文"秒"，不能用"s"**：

```
时长：4秒，比例：9:16
镜头一：……
```

---

## 高频踩坑速查（V-2/V-3/V-4/V-9/V-11，写 prompt 时直接复制对应禁忌句）

| 问题 | 典型现象 | 写 prompt 时加什么 |
|---|---|---|
| **V-2 字幕乱出** | 视频中无故出现错字字幕 | 末尾加：`保持无字幕，避免画面生成字幕`；输入图/视频先用 seedream/seedance 抹掉文字；横屏比竖屏概率低很多 |
| **V-3 logo/水印** | 蹦出 bilibili / 芒果TV 水印 | 末尾加：`不要生成logo，不要生成水印，不要文字` |
| **V-4 风格漂移** | 2D/3D 漫画漂成真人写实 | 头部加：`3D 国漫 CG 仙侠风格` / `2D 日漫风格`；参考图先转目标风格再用 |
| **V-9 数字滚动** | 数字翻牌效果跳变无序，文字约束无效 | 必须**用参考视频**喂数字滚动效果（`@视频1的数字效果`），文字描述无效 |
| **V-11 胡言乱语** | 视频时长 > 有效台词时容易胡说 | 缩减视频时长到匹配台词；竖屏改横屏；不要让模型"自由发挥"塞额外台词 |

> 详细解法见火山官方《Seedance 2.0 R2V FAQ》各 V 章节。

---

## 分镜参考结构（带主体定义示例）

```json
{
  "title": "月下抚琴",
  "duration_total": 30,
  "ratio": "16:9",
  "style": "唐风武侠，水墨质感",
  "synopsis": "...",
  "entities_planned": [...],
  "shots": [
    {
      "id":"s01","duration":6,"scene":"courtyard","characters":["protagonist_li"],
      "props":["guqin"],
      "camera_movement":"中景缓推（镜头）",
      "subject_action":"李慕白盘膝抚琴，指尖缓拨琴弦（主体）",
      "spatial":"庭院石台中央，月光自左上洒下（空间）",
      "audio":"<指尖拨动琴弦的清音> <夜风拂叶> {（无台词）}",
      "chain_with": null
    }
  ]
}
```

> shot 字段用 **camera_movement / subject_action / spatial / audio** 四件套对齐"动态描述顺序"——agent 拼 prompt 时按 `f"{camera_movement}。{subject_action}。{spatial}。{audio}"` 直接拼，节奏就对了。
> ⚠️ `audio` 字段**只允许 `<音效>` + `{台词}`**，禁止写 `（BGM）`——BGM 走整片后期 `audio_amix`。
