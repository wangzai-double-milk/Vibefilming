# Skill · 音频与配乐（Audio & BGM）—— Seedance 出对话/音效，BGM 走后期 BigMusic

> **何时读我**：任何要出"带声音"的视频之前；要做 BGM / 台词 / 配乐衔接 / MV 时必读。
> **关键工具**：`gen_video_t2v(generate_audio=True, reference_audios=[...])` / **`gen_audio_bgm` + `query_audio_task`**（火山 BigMusic）/ `audio_amix` / `video_concat` / `video_fade`
> **来源**：本 skill 浓缩自火山引擎官方《Seedance 2.0 R2V FAQ V1.7》+《BigMusic GenBGM v5.0》文档，并结合本项目踩坑总结。

---

## 头号铁律 ⭐⭐⭐（项目级硬约束）

> **Seedance 只出"对话 + 音效"，BGM 一律不让 Seedance 生成。**
> **BGM 全部走"成片完成 → BigMusic 出 BGM → 后期一次性 `audio_amix` 整片铺底"路线。**

| 维度 | Seedance 负责 | BigMusic（gen_audio_bgm）负责 | 后期 ffmpeg 负责 |
|---|---|---|---|
| 角色台词 / 旁白 | ✅（4 类符号 `{}`） | ❌ | ❌ |
| 现场音效（脚步/风声/狗叫） | ✅（4 类符号 `<>`） | ❌ | ❌ |
| 背景音乐 BGM | ❌ **严禁** | ✅ 整片一次性出一首 | ❌ |
| 整片铺底 | ❌ | ❌ | ✅ `audio_amix` |
| 段间音乐连贯性 | 不需要 | 整片就一首，天然不存在 | ✅ |

**为什么这样切分**：
- Seedance 单段 BGM 各段调性/节奏/响度不一，**怎么拼都会断**
- BigMusic 一次出整片 BGM → 整片就一首、一种响度，**根本不存在"段间断裂"问题**
- Seedance 解放出算力专注做对白和环境音，反而更稳

---

## ⚠️ BGM 调用时机（铁律 · 必须最后一步）⭐

**`gen_audio_bgm` 只能在「整片视频拼接完成 + 已过 vlm 审片」之后调用**，禁止：

- ❌ 一开始就把 BGM 出好（你都不知道成片实际多长 / 有没有重做）
- ❌ 每个 shot 出一首 BGM 然后拼（段间一定断裂）
- ❌ 不过 vlm 就预生成 BGM（万一片子要重做，BGM 白烧）
- ❌ **amix 后不 review 直接交付**（违反"分镜=交付契约"，BGM 跟画面情绪不搭也是交付事故）

**正确流程顺序**：

> ⭐ **命名白名单铁律**：`video_concat` 输出的成片 `name=` **必须**叫 `final_no_bgm`（不带 BGM 的干净底片），`audio_amix` 输出 `name=` **必须**以 `_with_bgm` 结尾。**禁止**取 `final_30s_dance` / `final_v1` / `final` 等任何不含 `_no_bgm` / `_with_bgm` 后缀的含糊名——含糊名 = agent 自己也忘了 BGM 还没配 = 跳过整个阶段 5 直接交付。**已踩坑：p20260601_133747_ 跳舞项目** agent 用 `final_30s_dance` 命名 → 直接 vlm 审无 BGM 成片 → start_long_term_update 交付，BGM 流水线一行没调。

```
1. 所有 shots 全部生成 + vlm 全过审
2. video_concat 拼成 final_no_bgm.mp4   ← 名字必须是这个白名单
3. agent 自己判断：本片需不需要 BGM？需要什么风格的 BGM？
4. gen_audio_bgm(prompt=<≥50字风格描述>, base_video="composed/final_no_bgm.mp4",
                 name="bgm_main")
   ↳ 传 base_video 后工具自动 probe 成片时长并 clamp 到 [30,120]，
     等长生成，agent 不用自己算 duration（火山硬限制 30-120s，超过 120s 也只能给 120s）
5. query_audio_task(task_id, save_name="bgm_main") → 拿到 audios/bgm_main.mp3
6. audio_amix(base_video=final_no_bgm.mp4, bgm_audio=audios/bgm_main.mp3,
              bgm_volume=0.15-0.25, name="final_with_bgm")
7. video_fade(fade_out=0.5) 兜底末尾噪音 → final_v1.mp4
8. ⭐ vlm_understand(final_v1, name="review_final_with_bgm",
                     question="<标准 review 三段式问题，重点问 BGM 是否贴合情绪/不盖对白/无突兀>")
9. review 结论：
   - 过审 → start_long_term_update / 交付
   - 不过审 → 按 review 反馈重写 BGM prompt（不重做画面），
              gen_audio_bgm 起一个新 name 如 bgm_main_v2，回到第 5 步
   - 同一片最多返工 2 轮 BGM，2 轮过不了标 best_effort 顶上
```

> **成片 >120s 怎么办？** 火山 GenBGM 单次硬上限 120s。本项目策略：**不做循环铺底**（实测循环点听感差），改用"等长直出 ≤120s"。如果成片 >120s（罕见），先 ask_user 确认是否切短片或拆段做多片配乐。

### 兜底：BGM amix 后审什么 ⭐

review_final_with_bgm 的 question 必须覆盖以下 4 项（参考 [skill_director_vlm.md](../skill_director_vlm/skill_director_vlm.md) 三段式模板）：

| 审什么 | 不过审示例 | 修正方向（写到 prompt v2） |
|---|---|---|
| **情绪贴合度** | 公益警示片配了欢快尤克里里 | 强调"压抑 / 反思 / 沉重"基调 |
| **音量平衡**（不盖对白） | BGM 把人声完全盖了 | 调小 `bgm_volume` 到 0.10-0.15，重做 amix |
| **段间感受** | BGM 高潮跟画面低谷错位 | prompt 写明"前压抑中推进末释放"对应分镜节奏 |
| **末尾收束** | BGM 突然截断，没有 fade out | 末尾必走 `video_fade(fade_out=0.5)` |

> ⚠️ **不过审 = hard stop**：跟 shot review 一样的纪律，禁止"先交付再说"。BGM 不过审就重写 prompt 重做，不要靠 amix 调音量硬救。

---

## agent 自主决策：要不要 BGM？要什么样的 BGM？⭐

**禁止反复 ask_user "要 BGM 吗"**——你是导演，自己判断。决策标准：

### 何时需要 BGM
| 项目类型 | BGM 必要性 | 风格基调建议 |
|---|---|---|
| 公益警示片（霸凌/反诈/酒驾） | **强需要**，情绪推力 | 前压抑后反思，钢琴 + 低弦 / 静谧氛围 |
| 萌宠跳舞 / 生活 vlog | **强需要** | 温暖轻快，钢琴 + 木吉他 / 治愈系 |
| 武侠 / 动作 / 科幻短片 | **强需要** | 史诗感，鼓点 + 弦乐 / 电子合成 |
| 产品广告 / 概念片 | 中等需要 | 简洁现代，电子 / 极简钢琴 |
| 纯对话短剧（有完整台词） | **不要 BGM**（会盖掉对白）或极轻 0.10 | 仅在转场处淡入淡出 |
| MV 卡点视频 | 走 A-4 MV 模式（音乐先于画面） | 不在本 skill 范围 |

判不准 → 默认"需要 BGM"，按片子情绪基调起 prompt，不再问用户。

### BGM prompt 编写要素（≥50 字，含三要素）

火山 GenBGM 自己说"入参简单的 30s 短音乐容易触发版权校验（错误码 50000001）"，所以 **prompt 必须 ≥50 字**，且明确包含：

| 要素 | 例子 |
|---|---|
| **风格** | 电影配乐 / 治愈系 / 史诗感 / 极简钢琴独奏 / lo-fi hip-hop |
| **情绪曲线** | 前压抑后释放 / 始终温暖 / 紧张推进 / 平静悠远 |
| **乐器** | 钢琴主导 + 弦乐铺底 / 木吉他 + 雨声 / 鼓点 + 合成器 |
| **场景关联** | 校园警示片 / 萌宠日常 / 武侠对决 / 都市街头 |

**好的 prompt 范例**（公益警示片）：

```
30 秒校园霸凌警示片配乐。前 12 秒压抑沉重的钢琴单音 + 低频弦乐铺底，
营造压迫感；中段 12 秒鼓点逐渐推进，象征觉醒与反抗；末尾 6 秒
弦乐渐强收束于一个温暖大调和弦，象征希望与终结霸凌。无人声，
不要欢快旋律，整体调性偏电影 score 风格。
```

**烂的 prompt 范例**（必踩 50000001）：

```
轻松愉快的背景音乐  ← 13 个字，三要素一个都没有
```

### 用 Segments 控结构（可选高阶玩法）

不传 `segments` 时整段一首；想严格控段落用 `segments`，每段必填 `Name + Duration`：

```python
gen_audio_bgm(
  prompt="...", duration=30,  # 写了 segments 后 duration 由其总和决定
  segments=[
    {"Name": "intro",  "Duration": 6},   # 前奏
    {"Name": "verse",  "Duration": 12},  # 主歌
    {"Name": "chorus", "Duration": 8},   # 副歌
    {"Name": "outro",  "Duration": 4},   # 尾奏
  ],
  name="bgm_main",
)
```

`Name` 枚举值：`intro / verse / chorus / inst / bridge / outro`，每段 [5,120]，总和 [30,120]。

---

## 火山 BigMusic 异步两阶段（标准用法，跟 seedance 对称）

```python
# 阶段 1：提交（立即返回 task_id）
r = gen_audio_bgm(
    prompt="<≥50 字含风格/情绪/乐器/场景的描述>",
    duration=30,                # 30-120 秒；写了 segments 由其总和决定
    name="bgm_main",            # 落盘文件名 → audios/bgm_main.mp3
)
task_id = r["task_id"]

# 阶段 2：轮询（默认阻塞等待，30-90s 完成）
done = query_audio_task(task_id, save_name="bgm_main")
if done["status"] == "succeeded":
    bgm_path = done["path"]     # projects/<pid>/audios/bgm_main.mp3
    bgm_duration = done["duration"]  # 实际生成的秒数
elif done["status"] == "failed":
    # 处理 50000001 版权校验失败：丰富 prompt + 拉长时长 + 加 segments 重提
    raise ...
```

### 50000001 版权校验失败处置

prompt 太短/太"普通"会被火山版权校验挡掉。返工纪律：

1. 把 prompt 字数加到 ≥80 字
2. 显式标"原创编曲 / 电影 score / 极简独奏"等小众风格
3. 时长拉到 ≥45s（30s 是高危区）
4. 加 `segments` 让结构更具体
5. 仍然失败 → `enable_input_rewrite=True` 让模型自己改写一次再试

**最多返工 2 轮**，再不行就降级走"无 BGM 交付"，不要硬磕。

---

## prompt 编写直接铁律（gen_video_t2v 的 audio 部分）

**关键发现**（2026-05-29 篮球项目踩坑）：
- prompt 里**一个 `（）` 都没写**
- 但视频出来**还是有 BGM**
- 结论：`generate_audio=True` 时 Seedance 会**主动脑补 BGM**——"prompt 不写"**绝对**不等于"输出无 BGM"

**真正的 BGM 禁令是显式追加禁令句**——每次调 `gen_video_t2v(generate_audio=True)`，prompt 末尾**必须显式写**以下任一句：

```
无背景音乐，仅保留环境音效与人物对白
```
或
```
不要 BGM，不要配乐，只要现场音效和台词
```

同时遇到 `（）` BGM 描述一律删除或改写为环境音：

| ❌ 不要写 | ✅ 改写为 |
|---|---|
| `（轻快尤克里里 BGM）` | （删掉，加"无背景音乐"） |
| `（紧张鼓点配乐）` | `<远处沉闷鼓点声>`（改成环境音效） |
| `BGM: 摇滚` | （删掉） |
| `配乐：钢琴独奏` | （删掉） |
| 没有任何 BGM 相关字样 | ⚠️ 也要在 prompt 末尾加"无背景音乐"——别靠不写就 OK 的侥幸 |

---

## 4 类特殊字符规范（写 prompt 必遵守）

Seedance 看 prompt 时**靠这四类括号识别音频意图**——不写括号 = 模型自己猜，靠运气。

| 类型 | 符号 | 例子 | 本项目用法 |
|---|---|---|---|
| **音乐 / BGM** | `（）` | `（背景中播放着快节奏的摇滚乐）` | 🚫 **本项目禁用**——BGM 走后期 |
| **音效** | `<>` | `<远处传来狗叫声>` | ✅ 主力，丰富现场感 |
| **台词** | `{}` | `{你好，世界}`；小语种需标注：`用日语说道{こんにちは}` | ✅ 主力，对白必用 |
| **字幕** | `【】` | `【第一章：启程】` | ⚠️ 一般禁用（V-2 字幕乱出问题） |

> ❌ 反例：`BGM: 电子乐 / 音效: 鸟叫 / 旁白: "走吧"`
> ✅ 正例：`<远处鸟叫声> {走吧}`（不写 BGM）

### 时长 / 比例参数也要写进 prompt（双保险）

接口已经传了 duration / ratio，但**官方建议在 prompt 最前/最后再复述一遍**，且**用中文"秒"，不能用"s"**：

```
时长：4秒，比例：9:16
镜头一：……
```

---

## 标准音频流水线（项目级）

```
┌─────────────────────────────────────────────────────────┐
│  阶段 1 · Seedance 生成（每段独立）                     │
│  ─────────────────────────────────────                  │
│  prompt: <镜头><主体><空间> <环境音效> {对白}           │
│  generate_audio=True                                    │
│  reference_audios=[上一段 video_url]   ← 让对白音色连贯 │
│  ⚠️ prompt 不带 （） BGM 描述                           │
│                                                         │
│  产物：每段都有"对白 + 现场音效"，完全没有 BGM          │
└─────────────────────────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│  阶段 2 · 拼接（V-6 帧裁剪铁律）                        │
│  ─────────────────────────────────────                  │
│  video_trim 各段（前段 -6 帧，后段 -1 帧）              │
│  video_concat 拼成 final_no_bgm.mp4                     │
│  → 此时整片是干净的对白+音效底，没有 BGM 撕裂问题       │
└─────────────────────────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│  阶段 3 · 后期一次性铺 BGM（agent 自主决策 + 必审）      │
│  ─────────────────────────────────────                  │
│  ① 决定要不要 BGM（参考"agent 自主决策"决策表）         │
│  ② 不需要 → 跳过，直接进阶段 4                          │
│  ③ 需要 + 用户给了 mp3 → 直接 audio_amix                │
│  ④ 需要 + 自动生成（等长直出，不做循环）：              │
│     gen_audio_bgm(                                      │
│       prompt=<≥50 字 风格+情绪+乐器+场景>,               │
│       base_video="composed/final_no_bgm.mp4",           │
│       name="bgm_main")                                  │
│       ↳ 工具自动 probe 时长 clamp 到 [30,120] 等长生成   │
│       → task_id                                         │
│     query_audio_task(task_id, save_name="bgm_main")     │
│       → projects/<pid>/audios/bgm_main.mp3              │
│     audio_amix(                                         │
│       base_video=final_no_bgm.mp4,                      │
│       bgm_audio=projects/<pid>/audios/bgm_main.mp3,     │
│       bgm_volume=0.18,        ← 不要盖过对白           │
│       name="final_with_bgm")                            │
│  ⑤ video_fade(fade_out=0.5) → final_v1.mp4              │
│  ⑥ ⭐ vlm_understand(final_v1, name="review_final_with_bgm")│
│     ├ 过审 → 进阶段 5（交付）                           │
│     └ 不过审 → 改 BGM prompt 重做，最多 2 轮            │
└─────────────────────────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────┐
│  阶段 4 · 兜底（A-1 结尾噪音）                          │
│  ─────────────────────────────────────                  │
│  video_fade(fade_out=0.5)                               │
│  → 末尾 0.5s 平滑淡出，治"咔哒"截断音                  │
└─────────────────────────────────────────────────────────┘
```

---

## 多段对白音色连贯（reference_audios 共享）

**典型现象**：同一角色在 s01 / s02 / s03 三段中**音色变了**——s01 是清亮少女，s02 变成了沉稳御姐。

**解法（来自 A-3）**：

链式段从第 2 段起，把第 1 段的 `video_url` 同时塞给 `reference_video_url` 和 `reference_audios`：

```python
# 第 1 段：定调
q1 = gen_video_t2v(
    prompt="<本镜镜头><主体动作><空间> <脚步声> {小兔，今天我们去打篮球}",
    name="shot_s01",
    reference_entities=["bunny_player"],
    generate_audio=True,
    duration=8,
)
shot1 = query_video_task(q1["task_id"], save_name="shot_s01", duration=8)

# 第 2 段：承接
gen_video_t2v(
    prompt="承接上段视频，<快推>主体起跳扣篮 <球网响声> {好球！}",
    name="shot_s02",
    reference_entities=["bunny_player"],
    reference_video_url=shot1["video_url"],     # 画面承接
    reference_audios=[shot1["video_url"]],      # ⭐ 音色承接（同一个 url 既当画面参考又当音频参考）
    generate_audio=True,
    duration=6,
)
```

> Seedance 会从 reference_audios 里提取**音色/语气/节奏**，让本段新生成的对白与之衔接。
> 这一步只为**对白音色连贯**，不为 BGM。

---

## A-1 · 视频结尾噪音（"咔哒"截断音）

**典型现象**：视频末尾最后一两帧出现突兀的"咔哒"或"啪"，含人声时尤其明显。

**Seedance 2.0 已显著改善**，偶发时三选一：

1. **抽卡**：重新生成一次（出现概率不高）
2. **`video_fade(fade_out=0.5)`**：本项目首选，已内置音视频同步淡出
3. 剪映"音量包络线"功能手动拉关键点至 0

> ✅ 落地建议：**最后一段 mp4 落盘后默认走一次 `video_fade(fade_out=0.5)`** 兜底，再进 audio_amix。

---

## A-2 · 中文发音不准（多音字 / 形近字）

**典型现象**：特定字读错音，如"螭"读成别的；多音字没读对。

### 解法 1：替换为同音常见字（最简）

prompt 里把生僻字换成同音常见字：

| 想要 | 改写为 |
|---|---|
| `{螭龙山}` | `{吃龙山}` |
| `{棪木}` | `{燕木}` |

> 不能 100% 解决，但成本最低。

### 解法 2：TTS 先生成正确音频再当 reference_audios（最稳）

> ⚠️ 当前项目 `tts` 工具未配 key 禁用，本解法暂不可用。需要 100% 准确发音的项目要先开 TTS_APP_ID/TOKEN。

1. 用任意 TTS 工具先把台词生成 mp3/wav（**转成黑屏视频效果更稳定**）
2. 把这个黑屏视频当 `reference_audios` 喂给 Seedance
3. prompt 里写 `参考@音频1中的音频。<台词内容>`

---

## A-3 · 音色参考不准（妹音变御姐）

**典型现象**：参考音频是萌妹子，输出却是御姐——音色漂移。

**三种叠加解法**：

1. **prompt 里描述目标音色**（最有效）：
   - ❌ `使用@音频1的音色说："..."`
   - ✅ `使用@音频1低厚温润带细碎颗粒感中年男声的音色说："..."`
   - 音色形容词参考：`清亮少女`/`温润书生`/`沙哑老者`/`俏皮萝莉`/`沉稳御姐`/`磁性低音炮`
2. **台词风格贴近参考音频**：参考音频是日常聊天 → 生成视频的台词也用日常聊天，不要让参考音频是聊天但生成的台词是激昂演讲
3. 同一角色全片复用同一段参考音频，不要换来换去

---

## A-4 · MV 解决方案（音乐与画面严格对齐）

**仅当客户明确要求"做一个 MV / 卡点视频"时启用——本项目默认路线不走这条**，因为：
- 默认路线（Seedance 出对白音效 + 后期 amix BGM）已经能满足 90% 的"带 BGM 的短片"需求
- MV 模式要求"画面跟着音乐走"，会反向约束分镜的 duration 和动作设计，门槛很高

**官方解法（口型对齐成功率极高）**：

1. **将音频转成黑屏视频**（⚠️ 关键步骤——直接传音频不稳定，转成视频后稳定性大幅提升）：
   ```bash
   ffmpeg -f lavfi -i color=c=black:s=720x1280:d=<DUR> \
          -i input_music.mp3 \
          -shortest -c:v libx264 -c:a aac -map 0:v -map 1:a \
          music_blackvideo.mp4
   ```
2. **生成视频长度与输入视频严格一致**：duration 必须 = BGM 长度（不能多 1s 不能少 1s）
3. 把黑屏视频当 `reference_video_url`（不是 reference_audios，是 video_url！）

---

## 链式段的音频必查项（vlm_understand）

链式段 mp4 落盘后，除了画面承接，**必须问 vlm**：

- "本段角色的对白音色 / 语气，与上一段是否一致？"
- "本段现场音效（脚步、环境音）是否合理 / 与画面对应？"
- "结尾有没有'咔哒'截断音？"
- ⚠️ **不要问 BGM**——本项目 Seedance 不出 BGM

发现对白音色断裂 → 重做该段，传 reference_audios=[上一段 url]。
末尾噪音 → `video_fade(fade_out=0.5)` 兜底，不必重做。

---

## 当前工具状态

| 工具 | 状态 | 项目里怎么用 |
|---|---|---|
| `gen_video_t2v(generate_audio=True)` | ✅ | 默认开启，生成对白+音效 |
| `gen_video_t2v(reference_audios=[...])` | ✅ | 链式段 ≥2 段时必用，对白音色连贯 |
| `audio_amix` | ✅ | **整片合成最后一步**，统一铺 BGM（mp3/wav 输入） |
| `video_fade` | ✅ | 末尾兜底 A-1 噪音 |
| `gen_audio_bgm` | ✅ | **火山 BigMusic GenBGM v5.0** 提交 BGM 任务，返回 `task_id`；agent 自主决策要不要调 |
| `query_audio_task` | ✅ | 轮询 BGM 任务并落盘到 `projects/<pid>/audios/<save_name>.mp3`，给 audio_amix 当 bgm_audio |
| `tts` | ❌ 禁用 | 缺 TTS_APP_ID/TOKEN（A-2 解法 2 暂不可用） |

---

## BGM 素材来源（既然 Seedance 不出，那从哪来？）

按推荐度排序：

| 来源 | 优点 | 缺点 |
|---|---|---|
| **火山 BigMusic GenBGM（`gen_audio_bgm` + `query_audio_task`）** | 全自动、风格可控、跟项目主题强绑定（agent 自己写 prompt） | 需要 ≥45s + 50 字 prompt 才稳定，30s 短曲易踩 50000001 版权校验 |
| 用户/客户提供 mp3/wav 路径 | 风格 100% 命中预期、零生成成本 | 需用户配合 |
| 项目自带 BGM 库（`smoke_tests/outputs/*.aac` 等） | 立刻能用、零成本 | 数量少、风格固定 |
| 公版 / CC0 音乐库（freesound.org / pixabay） | 可下载、可商用 | 手动选曲不可自动化 |

agent 当前的标准做法（**不再 ask_user**）：

1. 用户**显式提供**了 BGM 路径 → 直接 `audio_amix`
2. 用户**显式说"不要 BGM"** → 走 `video_fade` 收尾交付，到此为止
3. 其他情况 → **agent 自主判断**（参考上文"agent 自主决策"决策表）：
   - 决定要 BGM → 调 `gen_audio_bgm` + `query_audio_task` 生成 → `audio_amix`
   - 决定不要 BGM → 直接 `video_fade` 收尾交付

---

## 速查决策树

```
要出带声音的视频
│
├── 单段 ≤15s
│   └── generate_audio=True
│       prompt 只用 <音效> {对白}，禁用 （BGM）
│       → 单段直接交付（如需 BGM 跳到下方"整片 amix"）
│
├── 多段链式（≥2 段）
│   ├── 第 1 段：generate_audio=True，无 reference_audios
│   ├── 第 N 段（N≥2）：generate_audio=True，
│   │              reference_video_url=第1段url，
│   │              reference_audios=[第1段url]   ← 对白音色连贯
│   ├── 拼接前先做 V-6 帧裁剪（前段-6/后段-1）
│   └── video_concat 出干净底片
│
├── 整片要 BGM（不论 1 段还是多段）
│   ├── 用户给了 mp3 路径 → 直接走 audio_amix
│   ├── agent 自主判断要 BGM
│   │   ├── 写 ≥50 字 prompt（风格+情绪曲线+乐器+场景关联）
│   │   ├── gen_audio_bgm(prompt, base_video=final_no_bgm.mp4, name)
│   │   │   ↳ 工具自动 probe 时长并 clamp 到 [30,120]，等长生成
│   │   ├── query_audio_task(task_id, save_name) → 落盘 audios/<name>.mp3
│   │   ├── audio_amix(bgm_audio=audios/<name>.mp3, bgm_volume=0.15-0.25)
│   │   ├── video_fade(fade_out=0.5)
│   │   └── ⭐ vlm_understand review_final_with_bgm 必审
│   │       不过审 → 重写 BGM prompt → 起 name v2 重做（最多 2 轮）
│   └── agent 判断不需要 BGM → 跳过此分支，直接走 video_fade 收尾
│
├── 末尾"咔哒"噪音
│   └── video_fade(fade_out=0.5)
│
└── 客户明确要做 MV / 卡点视频
    └── 走 A-4 MV 模式（外部 BGM 转黑屏 → reference_video_url + 严格等长 duration）
```
