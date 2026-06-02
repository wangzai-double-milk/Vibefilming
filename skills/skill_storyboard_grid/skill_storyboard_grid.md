# Skill · 分镜关键帧并行法（Storyboard Grid / Parallel Keyframes）

> **何时读我**：分镜契约已锁定，且片子属于"一镜一画面、镜头之间是切换而非连续运动"类型（叙事广告 / MV 卡点 / 产品分镜），想**并行出片提速**时。
> **关键工具**：`gen_image`（出关键帧）/ `gen_video_t2v`（带 `reference_images` 做关键帧驱动）/ `query_video_task`（并行轮询）
> **本 skill 是 `skill_video_chain` 的并列替代路线，不是默认。** 二选一，按片子类型选。

---

## 它解决什么（为什么能并行）

链式承接（`skill_video_chain`）里，第 N 段要吃第 N-1 段的 `reference_video_url` 来承接画面/光影 → **段与段串行依赖**，第 2 段必须等第 1 段出完才能提交，最坏耗时 N×T。

本方法把"跨镜头一致性"从**视频层**前移到**图层**：先用一组关键帧图把每个镜头的画面定死（风格/角色/构图在图层就对齐），再让每段视频各自以**自己那张关键帧**作 `reference_images` 起跳。各段不再依赖上一段视频 → **依赖链断开 → 全部并行提交，只等最慢那段。**

> ⚠️ **这是 trade-off，不是纯赚**：换来速度，牺牲段间"连续动作/运镜"的连贯（每段从静态关键帧各自起跳，段间运动可能一顿一顿）；段间对白音色也带不过去。所以只用于镜头之间本就是"切"的片子。

---

## 适用判断（先确认该不该用本路线）

| 片子特征 | 走哪条路线 |
|---|---|
| 镜头之间是**切换**（叙事广告、MV 卡点、产品分镜、图文短片） | ✅ 本 skill（并行） |
| 强调**一镜到底 / 连续动作承接**（武打、追逐、长镜头运动） | ❌ 走 `skill_video_chain`（链式承接连贯性丢不得） |
| 单段无承接 | 都行，单段无所谓并行 |

---

## 前置：仍受铁律约束（不要以为换了路线就能跳步）

本路线只改"段间怎么承接"，下列状态底线照常生效（见 sys_prompt 机制铁律）：

- **铁律1 分镜契约先行**：关键帧本身就是一种合法的分镜契约形态——但仍要先锁定镜头数/每镜时长/出场主体，列了几段交几段。
- **铁律2 主体先于成片**：关键帧图里的角色/道具/场景必须来自**已建档并过审的 entity**，不是凭空画。出关键帧时用 entity 的 canonical 图作 `ref_image_url`，保证跨镜头同一张脸。
- **铁律3 Prompt 合规** / **铁律4 产物必审** / **铁律5 不过审 hard stop**：关键帧图、每段视频都要过 vlm 审。
- **铁律6 配乐是后期**：BGM 仍走 `skill_audio` 后期铺底。

---

## 标准编排（骨架，按项目细化）

```
1. 锁定分镜契约（镜头数 / 每镜时长 / 出场主体）            … 铁律1
2. 建 entity 并出齐视图、逐张过审                          … skill_entity_consistency（铁律2）
3. 出关键帧图（每个镜头一张，或一张九宫格切格）：
     gen_image(prompt=该镜头画面, ref_image_url=该镜头主体的 entity 图)
   ↳ 关键帧之间风格/角色靠 ref_image_url 锁定 → 这一步决定了最终一致性
   ↳ 逐张 vlm 审：构图对不对、角色一致不一致（不过审重出，别带病进下一步）  … 铁律4/5
4. 【并行】每段视频各自以自己的关键帧起跳，一次性全部提交：
     for shot in shots:
         gen_video_t2v(
             prompt=该镜头的动作/运镜/音频描述,
             reference_images=[该镜头关键帧图 url],     ← 关键：驱动源是图，不是上一段视频
             reference_entities=[本镜出场的人/物/景],   ← entity 双保险
             duration=该镜头时长)
         → 立即拿 task_id，先不 query
5. 【并行】依次 query_video_task(task_id, save_name) 落盘各段          … skill_async_schedule
6. 每段 vlm 审；不过审的单独重做（其它段不受影响，这正是解耦的好处）  … 铁律4/5
7. 全段过审 → video_concat 拼接 → 走 BGM 后期 → 复审 → 交付       … skill_audio
```

> 与链式法的唯一结构差异在 **step 4**：`reference_images=[关键帧]` 取代了 `reference_video_url=[上一段]`。其余流程（建档、审片、拼接、配乐）完全复用。

---

## 关于"九宫格切格"的取舍

可以用**一张九宫格图**一次性出 9 个镜头的关键帧（风格天然统一、省图次），但：

- ⚠️ **分辨率陷阱**：一张图切 9 格，单格分辨率只有整图的 1/9，作 `reference_images` 输入可能糊、细节丢失。
- **折中**：九宫格只当"构图/风格的总览参考"，真正喂给 `gen_video_t2v` 的关键帧建议**逐张单独出**（`gen_image` 一镜一张全分辨率），用九宫格图当这批单张图的 `ref_image_url` 来锁风格。
- 镜头数 ≤ 4 且画面简单时，直接九宫格切格也够用，按预算与清晰度要求权衡。

---

## 并行度与预算

- 并行提交不省 Seedance 调用次数，只省**墙钟时间**；预算（`max_seedance_calls`）该怎么估还怎么估，见 `skill_async_schedule`。
- 关键帧图额外消耗 `gen_image` 次数（每镜一张），换来视频层可并行——对镜头多的项目，时间收益通常远大于这点图成本。

---

## 跨 skill 引用

- 分镜契约字段标准 → [skill_storyboard.md](../skill_storyboard/skill_storyboard.md)
- entity 建档与一致性 → [skill_entity_consistency.md](../skill_entity_consistency/skill_entity_consistency.md)
- prompt 句式 / 4 类括号 / 踩坑速查 → [skill_prompt_engineering.md](../skill_prompt_engineering/skill_prompt_engineering.md)
- 并行提交+轮询调度 → [skill_async_schedule.md](../skill_async_schedule/skill_async_schedule.md)
- 审片模板 → [skill_director_vlm.md](../skill_director_vlm/skill_director_vlm.md)
- 链式承接（本 skill 的并列替代） → [skill_video_chain.md](../skill_video_chain/skill_video_chain.md)
- 后期配乐 → [skill_audio.md](../skill_audio/skill_audio.md)
