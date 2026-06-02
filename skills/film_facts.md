# Film 事实参考卡（Facts / Reference）

> **这里只放 agent 凭 schema + skill 推不出来的「硬事实」：模型 ID、产物目录、日志语义。**
> - 单个工具怎么调（入参/出参）→ 看 function calling 注入的工具 schema。
> - 单个领域的方法论（为什么这么做、几次、踩什么坑）→ 看 `skills/skill_*/`，按场景自取。
> - 该读哪个 skill → 看 sys_prompt 里自动生成的 skill 路由表（`{{SKILLS_INDEX}}`）。
> - **强制约束（如 entity 未出齐不许出片、BGM 一律后期）在 sys_prompt 的「机制铁律」里**，以那为准，本文件不重抄。
> - 工具之间的依赖顺序（先建 project 再排分镜、先出 entity 再出 shot…）由各 skill + schema 的依赖关系决定，agent 自行编排，本文件不写死分步流水线。

---

## 模型清单（你能调动的视觉/文本智能）

| 模型 | 模型 ID | 能力 |
|---|---|---|
| **Doubao Seed 2.0 pro** | `doubao-seed-2-0-pro-260215` | 文本 + 多图理解 + 视频原生理解（不抽帧） |
| **Doubao Seedream 4.0** | `doubao-seedream-4-0-250828` | 文生图 / 图编辑（带 ref_image_url） |
| **Doubao Seedance 2.0** | `doubao-seedance-2-0-260128` | 文生视频，**只走多模态参考模式**（reference_images / reference_video_url） |

---

## 项目目录结构（`project_create` 产出）

```
projects/<pid>/
├── manifest.json        ← phase / budget / storyboard 摘要 / entities 摘要
├── storyboard.json      ← 分镜（storyboard_set 写入）
├── entities/            ← 角色/道具/场景档案库
│   ├── protagonist_li/          ← character
│   │   ├── ref.json             ← description / canonical_view / views
│   │   └── turnaround.png       ← canonical（白底三视图 front/side/back 合一）
│   └── sword_qingfeng/          ← prop
│       ├── ref.json
│       ├── sheathed.png         ← canonical（入鞘）
│       └── drawn.png            ← 出鞘
├── shots/               ← 单镜头图/视频
├── composed/            ← 拼接成片
├── audios/              ← BGM 落盘
├── reviews/             ← vlm_understand 审片结果
└── logs/                ← 审计流水（见下）
```

---

## 审计日志三件套（复盘调用流程时看）

| 文件 | 视角 |
|---|---|
| `tool_calls.jsonl` | GA 框架视角，所有工具调用入参/出参摘要 |
| `seedance_calls.jsonl` | Seedance 专项，每段视频用了哪些参考图/参考视频 |
| **`model_calls.jsonl`** ⭐ | 所有云端模型调用的**全量请求**（含完整 prompt 不截断）+ 参数 + 结果。**复盘优先看这个** |

---

## GA 通用工具（继承，跨模式可用）

`code_run` / `file_read` / `file_write` / `file_patch` / `web_scan` / `web_execute_js` / `update_working_checkpoint` / `ask_user` / `start_long_term_update`
