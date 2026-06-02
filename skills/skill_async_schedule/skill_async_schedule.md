# Skill · 异步任务 + 调度策略（Async & Scheduling）

> **何时读我**：要批量出图/出视频时；要决定串行还是并行时；预算紧张时。

---

## Seedance 异步任务的姿势（最容易踩的坑）

- 提交 `gen_video_t2v` 后**立即** `query_video_task(task_id, save_name, duration=本镜时长)`，它会按 ETA 自动等到完成 + 打印进度条。**强烈建议传 duration**，让 ETA 准（10s 视频约 210s，15s 视频约 285s）
- **不要"输出一句『等 30 秒再查』然后停"** —— agent 输出纯文本就 = 任务结束、循环不会自己醒
- query 是阻塞的，agent 在 query 期间不会做别的，所以**多任务并行的关键是先全部 submit 再依次 query**

---

## 串行 vs 并行 决策表

| 情形 | 走法 | 原因 |
|---|---|---|
| 多个分镜**有链式衔接**（第 N 段需要第 N-1 段视频 url） | **必须串行** | reference_video_url 依赖上一段产出 |
| 多个分镜**完全独立**（如多片段花絮拼盘） | **并行** submit + 依次 query | 单段 200-300s，并行只等最慢的那个 |
| character 三视图（front/side/back） | 可串行可并行 | 后面视图依赖 canonical 的 url，串行更稳；嫌慢就并行（可能损失一致性） |
| 多个独立 entity 的基准图 | **并行** | entity 之间无依赖 |

---

## 链式必串行的硬约束

> 第 N 段（N≥2）需要把上一段视频 url 当 `reference_video_url`，所以：
> 1. 上一段不出 mp4 不能提交下一段
> 2. **上一段不过 vlm 检查不许提交下一段**，否则崩坏会累积传染

详细链式打法见 [skill_video_chain.md](../skill_video_chain/skill_video_chain.md)。

---

## 预算守门（Seedance 真金白银）

- Seedance 单段 200-300s，每次 `gen_video_t2v` 扣减 `seedance_used` 预算 +1
- `max_seedance_calls` 默认 20，分镜 self_review 第 5 条要算 `len(shots) × 1.2 重做率` 不超过它
- **预算跑掉一半还没出初版 → 停下来 ask_user 对齐**（这是 ask_user 的合法触发条件之一）

## 省预算的常用手段

| 手段 | 收益 |
|---|---|
| 草图/迭代验证用 480p，常规交付 720p，终片才 1080p | 30-50% 成本 |
| 图编辑（带 ref_image_url 的 gen_image）优先于重画 | 一致性更好 + 不浪费预算（图便宜） |
| 非链式段并行 submit + 依次 query | 时间省一半 |
| 同一 shot 返工 2 轮过不了 → best_effort 放行 | 防"无底洞磨片" |
| `generate_audio=True` 只在本段确实需要对白/音效时开（默认 False 完全不出音轨） | 30%/段 成本；整片 BGM 是阶段 5 用 `gen_audio_bgm` 单独生成的，跟 `generate_audio` 无关 |

---

## 失败重试策略（继承 GA）

- 失败 1 次 → 读错误，2 次 → 探测环境，3 次 → 换方案。**不重试同 prompt**
- `content_violation`（敏感词）→ 改词；连续 2 次还过不去 → ask_user
- `rate_limit` → 等 60s 后重试
- 其他报错 → 直接告知用户

---

## sleep 工具的边界

`sleep(seconds)` 上限 120s，**几乎用不上** —— `query_video_task` 已经阻塞等。
仅当需要给外部任务留时间（如手动等用户操作）时使用。
