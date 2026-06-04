<div align="center">

# 🎬 VibeFilming

### 一句 Brief，一部短片。AI 导演自己分镜、自己出片、自己审、自己改。

**你只管说一句话——「给我做个 30 秒校园霸凌警示公益片」**
**它做：分镜 → 出图 → 出视频 → VLM 审片 → 拼片 → 配 BGM → 交付**

*基于 [GenericAgent](https://github.com/JinyiHan99/GA-Technical-Report) 框架 · ARK + BigMusic 双引擎 · 全流程自主决策*

---

🎨 Seedream 4.0 出图　🎬 Seedance 2.0 出视频　👁️ Seed 2.0 当导演审片　🎵 BigMusic 自主配乐

</div>

---

## ✨ 它能做什么

| 你说一句 | 它干这些活 |
|---|---|
| "做个 30s 公益小视频" | 分镜（4 镜）→ 4 个角色档案 → 4 段视频 → 拼片 → 暖色 BGM → 交付 mp4 |
| "宠物跳舞 15 秒，竖屏" | 分镜（3 镜）→ 萌宠 turnaround 基准图 → 3 段动态 → 9:16 竖屏拼接 → 欢快电子 BGM |
| "武侠片 60s，刀光剑影" | 分镜（5 镜）→ 主角三视图 + 兵器立绘 → 5 段链式承接 → 概念片节奏 BGM |
| "30s 海边温情" | 自动判断不需要对白 → 海浪原生音 + 钢琴铺底 BGM |
| **"30s 咖啡广告，并行出片"** | **九宫格关键帧法**：一次性出 N 张关键帧 → 多段视频并行提交 → 全过审拼片 |

**全程不需要你介入**——它会自己想分镜、自己写 prompt、自己审片不过审就重做、自己判断需不需要 BGM 配什么风格的 BGM。

> 📖 **第一次用？先看 [DIRECTOR_GUIDE.md](./DIRECTOR_GUIDE.md)**——给导演看的人话手册：每个工具干嘛用、想改它的拍法去改哪个文件、0 代码加新技能。

**你只在两种情况下被打扰**：①brief 实在没说清 ②预算烧到一半还没出片需要确认。

---

## 🔥 凭什么它能"自主"？

我们没用任何工作流编排框架。整套系统就一个 **GA 主循环 + 36 个原子工具 + 一堆经验文档**。
让 agent 自己变聪明的核心是这三件事：

### 1. 「导演式自审」 —— 每个产物 VLM 当导演审一眼

不是 QA 验收题（"是否符合 A+B+C+D"），而是导演审美题（"作为这部片的导演你看上了哪些点 / 你觉得哪里不对"）。

```
✅ 过审 → 进下一步
❌ 不过审 → 给"导演视角的问题诊断+想要的样子" → agent 走 PE 7 步翻译成合规 prompt → 重做（最多 2 轮）
```

按产物类型分 **A 基准图 / B 单镜头 / C 全片成片** 三套模板，每套都有专属的导演关注重心。

### 2. 「Prompt Engineering 7 步 + 5 大死罪」—— prompt 不是 prompt 是工程产物

每次喂给 Seedance/Seedream 之前，agent 必须按 PE checklist 过 7 步：任务类型 / 主体定义 / 动态顺序 / 4 类符号 / BGM 闸门 / 时长比例 / 反例扫描。**5 大死罪一个都不许踩**：

| # | 死罪 | 翻车现场 |
|---|---|---|
| 1 | ≥3 主体未定义 | 三个垃圾桶分不清谁是谁 |
| 2 | 主体特征定后变 | 「红裙女孩」开头红裙结尾蓝裙 |
| 3 | 多素材未绑定 | 「老奶奶 + 小女孩 + 垃圾桶」三视图但 prompt 只写「老奶奶」 |
| 4 | 描述顺序乱 | 主体写在镜头之前，画面歪 |
| 5 ⭐ | **张嘴无声** | 写「老奶奶夸奖」没补 `{真棒！}` → 演员张嘴没声音演哑剧 |

### 3. 「BGM 必须走后期」—— Seedance 不脑补 BGM

| 约束 | 机制 |
|---|---|
| 默认值 | `generate_audio=False`，模型完全不出音轨 |
| prompt 纪律 | 禁止写`（轻快 BGM）`/`配乐：xxx`；环境音改写成 `<音效>`（由 skill_audio / skill_prompt_engineering 约束 agent 自觉遵守） |
| 开音轨场景 | 必须出原生对白唇形时才开 `generate_audio=True`，并在 prompt 末尾追加"无背景音乐，仅保留环境音效与人物对白" |

**整片 BGM 的唯一入口** = 流水线最后一步的 `gen_audio_bgm`（火山 BigMusic GenBGM v5.0），由 agent 自主判断要不要配 + 配什么风格。

---

## 🚀 三步跑起来

### 1. 装环境

```bash
bash setup.sh
```

脚本会：检查 Python 3.11/3.12 → 建 `.venv` → 装依赖 → 复制 `mykey.example.py` → `mykey.py`。

### 2. 填 API key

编辑 [mykey.py](./mykey.py)：

**必填** —— 豆包 ARK（出图/出视频/VLM 审片）：
```python
'apikey': 'ark-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX',
```

**可选** —— 火山 BigMusic（自动配 BGM）：
```python
volc_open_api_config = {
    'VOLC_AK': 'AKLT...',
    'VOLC_SK': '...==',
    ...
}
```

不填 BigMusic 也能跑，只是不能自动配 BGM（手动塞 mp3 走 `audio_amix` 也行）。

详细开通流程见 [mykey.example.py](./mykey.example.py) 里的注释。

### 3. 启动

```bash
source .venv/bin/activate
python3 agentmain.py
```

进 REPL 后直接说人话：

```
> 给我做一段关于宠物的温情小视频
> 生成一段 30s 的科幻武侠片
> 帮我剪一个 15 秒夏日海边短片，竖屏，海浪原生音 + 钢琴 BGM
```

退出：`Ctrl+C` 或 `/exit`。

---

## 🎞️ 端到端工作流

```
你的 brief
    ↓
[阶段 1] 规划镜头（agent 自己拍板）
         └─ 两种路线二选一：
            · 结构化分镜（file_write 写 storyboard.json）→ 链式承接，连贯性强
            · 九宫格关键帧（skill_storyboard_grid）→ 关键帧并行，速度快
    ↓
[阶段 2] 用 gen_image 出角色/道具/场景的参考图
         └─ 每张参考图必过 vlm_understand（导演审片），记住「主体名 → url」
    ↓
[阶段 3] 每个 shot：PE 7 步 → gen_video_t2v → query_video_task
         └─ 链式路线串行承接 / 九宫格路线并行提交
         └─ 每段必过 vlm_understand（不过审就重做，最多 2 轮）
    ↓
[阶段 4] 所有 shot 全过审 → video_concat → final_no_bgm.mp4
    ↓
[阶段 5] 🎵 agent 自主决策要不要 BGM、配什么风格
         └─ gen_audio_bgm → query_audio_task → audio_amix → review_final_with_bgm
    ↓
[阶段 6] 🎁 交付 final.mp4
```

---

## 📁 输出在哪

```
projects/<project_id>/
├── manifest.json          ← 项目元信息 / 预算 / 状态
├── entities/              ← 角色/道具/场景基准图
├── shots/                 ← 每个 shot 的 mp4 + 关键帧
├── composed/
│   └── final_xxx.mp4      ← ⭐ 最终交付
├── audios/                ← BGM mp3
├── reviews/               ← 每次 VLM 导演审片的问答记录（json）
└── logs/
    └── tool_calls.jsonl   ← 完整工具调用日志（含烧钱总账）
```

---

## 🛠️ 工具清单（29 个原子工具）

| 类别 | 工具 |
|---|---|
| 🗂️ 工作区 | `project_create` / `project_open` |
| 🎭 主体参考图 | 用 `gen_image` 出角色/道具/场景参考图 → `reference_images` 喂给视频（无专门 entity 工具，方法见 skill） |
| 🎨 视觉生成 | `gen_image` / `gen_video_t2v` / `query_video_task` / `cancel_video_task` |
| ✂️ 视频处理 | `video_concat` / `video_crossfade` / `video_trim` / `video_speed` / `video_overlay` / `video_fade` / `video_portrait` / `burn_subtitle` |
| 🎵 音频 | `gen_audio_bgm` ⭐ / `query_audio_task` ⭐ / `audio_amix` / `tts` |
| 👁️ 评估 | `vlm_understand`（导演视角审片，三场景模板）/ `extract_frames` |
| 🧰 通用 | `code_run` / `file_read` / `file_write` / `file_patch` / `web_scan` / `web_execute_js` / `update_working_checkpoint` / `ask_user` / `start_long_term_update` |

> 📌 分镜/状态查询**不再是独立工具**——分镜用通用 `file_write`/`file_read` 读写 `projects/<pid>/storyboard.json`，项目状态用 `file_read` 读 `manifest.json`。

> 🆕 **导演视角的工具说明（人话版）见 [DIRECTOR_GUIDE.md](./DIRECTOR_GUIDE.md)**——每个工具干嘛、什么时候用、想改拍法改哪里。
> 模型清单 / 目录结构 / 审计日志等硬事实已内联进 [assets/sys_prompt_film.txt](./assets/sys_prompt_film.txt) 的「事实参考卡」段。

---

## 🧠 设计原则

- **导演视角自审**：VLM 不打分、不列维度、不写 QA 报告，按导演审美发散性评判
- **VLM ≠ PE 工程师**：VLM 只出"问题诊断+想要的样子"，agent 走 PE 7 步翻译成工程化 prompt
- **流程不钉死**：没有"必须 P1→P2→P3"的硬流程，agent 根据 review 反馈自己跳回重做 / 跳过 / 换思路
- **工具纯原子 + 文档层经验沉淀**：tool 只做纯 API 调用（不夹业务判断/编排 hint）；所有方法论、纪律、流程编排都沉淀进 skill md，由 agent 自觉遵守
- **铁律 = 状态底线，不规定路线**：sys_prompt 只说"必须达到什么状态"（不偷砍 / 主体过审 / 产物过审 / 不脑补），不绑工具名/格式；**怎么实现是 skill 的事**——这样新增/替换实现路线（如九宫格并行）0 改 system 即可生效
- **加 skill 0 代码生效**：在 `skills/` 下新建一个文件夹 + `SKILL.md`（对齐 Anthropic Agent Skills 标准：YAML frontmatter 写 `name` + `description`），开机自动进 SKILLS_INDEX
- **小步快跑**：单次只出最小可验证产物，VLM 看一眼再扩张
- **节约预算**：图编辑优先于重画，能并行就并行（链式承接除外）

完整 sys_prompt 见 [assets/sys_prompt_film.txt](./assets/sys_prompt_film.txt)，业务方法论沉淀在 [skills/](./skills/) 下各 `skill_*/` 文件夹（每个文件夹一个 `SKILL.md`，对齐 Anthropic Agent Skills 标准：YAML frontmatter + 正文），通用 SOP（plan / review / vision 等）以平铺 `.md` 文件并存于同一目录。

---

## ❓ 常见问题

**Q: 报 401 / 403**
A: `mykey.py` 里 apikey 没填对，或对应模型没在火山方舟开通访问权限。

**Q: BGM 报 200028 APINoSource**
A: 火山 BigMusic 服务没开通。去 https://console.volcengine.com/ai-music 开通"音乐生成"服务即可。

**Q: 报 ffmpeg not found**
A: `setup.sh` 装的 `imageio-ffmpeg` 自带 ffmpeg。如还报错：`brew install ffmpeg`（macOS）/ `apt install ffmpeg`（Linux）。

**Q: 视频任务长时间不返回**
A: Seedance 单段 200-300s 是正常的。`query_video_task` 最多阻塞 5 分钟，超时可重试。

**Q: agent 卡住没反应**
A: `Ctrl+C` 重启即可。项目状态都在 `projects/<id>/manifest.json`，不会丢。

**Q: 想换 LLM（不用豆包）**
A: 编辑 [mykey.py](./mykey.py)，通用模板见 [mykey_template.py](./mykey_template.py)。**但生图/生视频/VLM 必须用豆包**（代码里写死了模型 ID）。

---

## 🎮 REPL 控制命令

| 命令 | 说明 |
|---|---|
| `/new` | 开新对话清空上下文（项目文件保留） |
| `/continue` | 列出可恢复的会话快照 |
| `/llm` | 切换 LLM session |
| `/session.temperature=0.3` | 临时调采样温度 |
| `/exit` | 退出 |

---

## 📚 想深入研究？

| 文档 | 内容 |
|---|---|
| [DIRECTOR_GUIDE.md](./DIRECTOR_GUIDE.md) | 🆕 **导演手册（人话版）**：36 个工具说明 + 想改拍法去哪改 + 0 代码加新技能 |
| [skill_prompt_engineering](./skills/skill_prompt_engineering/SKILL.md) | PE 7 步 + 5 大死罪 + 4 类符号实战 |
| [skill_director_vlm](./skills/skill_director_vlm/SKILL.md) | VLM 当导演的三场景审片模板 |
| [skill_entity_consistency](./skills/skill_entity_consistency/SKILL.md) | 角色一致性 / 三视图 / 防 ID 漂移 |
| [skill_video_chain](./skills/skill_video_chain/SKILL.md) | 链式衔接 / 帧裁剪 / 防画质劣化 |
| [skill_storyboard_grid](./skills/skill_storyboard_grid/SKILL.md) | 🆕 **九宫格关键帧并行法**（适用多镜头切换片，不适用连续运动） |
| [skill_audio](./skills/skill_audio/SKILL.md) | BGM 决策 + BigMusic 接入 + prompt 纪律 |
| [skill_storyboard](./skills/skill_storyboard/SKILL.md) | 分镜设计 / entities_planned 必填 |

---

<div align="center">

**🎬 让 AI 当导演，让导演只说一句话。**

⭐ 觉得有用，给个 Star 支持一下

</div>
