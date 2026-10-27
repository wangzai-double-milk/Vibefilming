# VibeFilming 启动与环境配置指南

本文档面向第一次在本地把项目跑起来的人，讲清楚：**装什么环境、填什么配置、怎么启动前端（Studio 网页 + 实时画布）、常见坑怎么排。**

> 只想看产品能力和使用示例，请看 [README.md](./README.md) 和 [DIRECTOR_GUIDE.md](./DIRECTOR_GUIDE.md)。

---

## 一、环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | macOS / Linux | Windows 建议用 WSL2 |
| Python | **3.10 ~ 3.13**（`>=3.10,<3.14`） | 见 [pyproject.toml](./pyproject.toml)；推荐 3.12 |
| ffmpeg | 随依赖自带 | `setup.sh` 装的 `imageio-ffmpeg` 已内置，一般无需单独安装 |
| 网络 | 能访问火山方舟 API | `https://ark.cn-beijing.volces.com` |

必需的一把 Key：**火山方舟 `ark.api_key`**（同时驱动文本推理、VLM 审片、生图、生视频）。BGM 用的 `volc.ak/sk` 是选填。

---

## 二、安装（一键脚本）

```bash
cd "/你的项目路径/vibefilming-git"
bash setup.sh
```

[setup.sh](./setup.sh) 会自动完成 4 步：

1. 检测本机受支持的 Python（优先 `3.13 → 3.12 → 3.11 → 3.10`，拒绝 3.14+）；
2. 在项目下创建虚拟环境 `.venv`（若已有则跳过）；
3. 用 `.venv` 的 pip 安装依赖：`requests / beautifulsoup4 / bleach / bottle / simple-websocket-server / aiohttp / imageio-ffmpeg / mistune / prompt_toolkit / cozeloop`；
4. 若没有 `vibefilming.config.json`，从 `vibefilming.config.example.json` 复制一份模板并提示你去填 Key。

> ⚠️ **不要用 `python agentmain.py` 启动。** 如果你的 shell 里有 `alias python3=...`，即使 `source .venv/bin/activate` 后 `python3` 仍可能指向系统 Python。统一用 **`.venv/bin/python`** 最稳。

### 手动安装（不想用脚本）

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install requests beautifulsoup4 bleach bottle \
  simple-websocket-server aiohttp imageio-ffmpeg mistune prompt_toolkit "cozeloop>=0.1.28,<0.2"
cp vibefilming.config.example.json vibefilming.config.json
```

---

## 三、配置 `vibefilming.config.json`

配置文件由 [llmcore.py](./llmcore.py) 在启动时读取（路径写死在项目根目录）。最小可跑配置只需填 `ark.api_key`：

```json
{
  "ark": {
    "api_key": "ark-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
    "api_base": "https://ark.cn-beijing.volces.com/api/v3",
    "models": {
      "text":  "doubao-seed-evolving",
      "vlm":   "doubao-seed-evolving",
      "image": "doubao-seedream-5-0-260128",
      "video": "doubao-seedance-2-0-260128",
      "video_2_0": "doubao-seedance-2-0-260128",
      "video_2_5": "doubao-seedance-2-5-260628"
    }
  }
}
```

各字段含义：

| 字段 | 必填 | 作用 |
|---|---|---|
| `ark.api_key` | ✅ | 火山方舟 API Key，一把驱动全部模型 |
| `ark.api_base` | ✅ | 方舟接口地址，默认北京 region |
| `ark.models.text` | ✅ | Agent 推理 / 脚本规划 / 分镜拆解 |
| `ark.models.vlm` | ✅ | 看图看视频、导演式审片 |
| `ark.models.image` | ✅ | 角色三视图、场景图、关键帧、图编辑（Seedream） |
| `ark.models.video` | 选填 | 默认视频模型；兼容旧配置 |
| `ark.models.video_2_0` | 选填 | Seedance 2.0 模型 ID |
| `ark.models.video_2_5` | 选填 | Seedance 2.5 模型 ID |
| `volc.ak` / `volc.sk` | 选填 | 火山 BigMusic 生成整片 BGM；不填也能完整出片 |
| `cozeloop.*` | 选填 | 调用链路观测上报，默认 `enabled: false` |
| `feishu.*` | 选填 | 飞书机器人前端接入，见 [docs/SETUP_FEISHU.md](./docs/SETUP_FEISHU.md) |
| `workflow.default_execution_mode` | 选填 | 新项目默认执行模式：`human_gated` 或 `autonomous` |
| `server.studio.host` / `port` | 选填 | Studio 监听地址与端口，默认 `127.0.0.1:8764` |
| `server.canvas.host` / `port` | 选填 | 画布监听地址与端口，默认 `127.0.0.1:8765`，不继承 Studio 的对外地址 |
| `server.studio.public_url` / `server.canvas.public_url` | 选填 | 浏览器使用的 HTTP(S) 地址，可含反向代理路径前缀 |

**视频模型**：同时配置 `video_2_0` / `video_2_5` 时可在 Studio 按项目选择，
`video` 决定默认项；只配置一个时自动路由到该模型。模型选择写入项目 manifest，
不会影响其他并发项目。

参考视频拆镜的可选 Qwen-VL 兜底只在设置 `DASHSCOPE_API_KEY` 环境变量时启用；
模型和接口由当前实现固定管理，不在项目 JSON 中暴露无效配置项。

### 执行模式

```json
"workflow": {
  "default_execution_mode": "human_gated"
}
```

- `autonomous`：端到端无人干预。拆镜和剧本机器校验通过后自动推进到生成、合成与终审；只有缺少不可替代输入、合规/安全限制、提供方拒绝或最终失败才停止。
- `human_gated`：关键步骤人工介入。拆镜完成和剧本定稿后分别暂停，等待人工确认。

这是新项目默认值，优先级为 `VIBEFILMING_EXECUTION_MODE` > JSON > `human_gated`。新建项目时可在 Studio 覆盖；已有项目可在 Studio 的“执行模式”分段控件中实时切换，或让 Agent 调用 `set_execution_mode`。模式只控制是否暂停，不会跳过产物落盘、调用前校验、审查、调试停止点和安全规则。

---

## 四、启动前端（Studio 网页 + 实时画布）

激活后启动主程序即可，前端（本地 Studio 网页）会随进程自动起一个常驻 HTTP 服务：

```bash
.venv/bin/python agentmain.py
```

启动成功后终端会打印链接：

```
● VibeFilming Studio http://127.0.0.1:8764/   (左侧对话 · 右侧实时画布)
```

- **Studio 主界面**：默认 `http://127.0.0.1:8764/`，左侧对话、右侧工作流画布。
- **实时工作流画布**：跑起项目后由画布服务托管（默认端口 `8765`，见 [film/workflow_canvas.py](./film/workflow_canvas.py)），随进度自动刷新。

### 常用启动参数

参数定义见 [agentmain.py](./agentmain.py)：

| 命令 | 作用 |
|---|---|
| `.venv/bin/python agentmain.py` | 默认：终端对话 + 本地 Studio 网页 |
| `.venv/bin/python agentmain.py --open-studio` | 启动后自动用浏览器打开 Studio |
| `.venv/bin/python agentmain.py --no-studio` | 只用终端，不起 Studio 网页 |
| `.venv/bin/python agentmain.py --studio-port 8770` | 自定义 Studio 端口（默认 8764） |
| `VIBEFILMING_ALLOW_UNAUTHENTICATED_REMOTE_STUDIO=1 .venv/bin/python agentmain.py --studio-host 0.0.0.0` | 显式允许 Studio 非回环监听；仅可用于已有防火墙或认证代理隔离的环境 |
| `.venv/bin/python agentmain.py --canvas-host 127.0.0.1 --canvas-public-url https://example.com/canvas/` | 画布由反向代理对外提供 |
| `.venv/bin/python agentmain.py --task <dir> --input "<prompt>"` | 一次性任务模式（文件 IO，适合后台跑） |

启动后直接在终端里说需求即可，例如：

```
> 帮我做一段 30 秒的宠物温情短片，竖屏，结尾有领养呼吁
```

退出：`Ctrl+C` 或输入 `/exit`。

### 服务器部署：直接修改 JSON

在原配置中添加/修改 `server`，其他模型和密钥字段保留：

```json
"server": {
  "studio": {"host": "0.0.0.0", "port": 8764, "public_url": ""},
  "canvas": {"host": "127.0.0.1", "port": 8765, "public_url": ""}
}
```

本地 Studio 与 Canvas 都没有内置用户鉴权，因此非回环监听默认会被拒绝。确认已有防火墙或认证代理隔离后，设置 `VIBEFILMING_ALLOW_UNAUTHENTICATED_REMOTE_STUDIO=1` 才可启动上述 Studio。Canvas 默认继续监听本机，并由代理访问；如确需直接绑定公网接口，还要单独设置 `VIBEFILMING_ALLOW_UNAUTHENTICATED_REMOTE_CANVAS=1`。

配置优先级：**CLI > 环境变量 > JSON > 默认值**。两个服务均支持 `VIBEFILMING_STUDIO_HOST/PORT/PUBLIC_URL` 和 `VIBEFILMING_CANVAS_HOST/PORT/PUBLIC_URL`。CLI 对应 `--studio-host/port/public-url`、`--canvas-host/port/public-url`；配置在进程启动时生效，修改后重启。`public_url` 只生成浏览器链接，不会绕过监听安全门禁。

HTTPS 或反向代理部署时分别设置两个 `public_url`，例如 Studio 为 `https://example.com/film/`、Canvas 为 `https://example.com/canvas/`；代理将各自前缀转发并去掉前缀到对应监听端口。Canvas URL 可写目录前缀或完整 `canvas.html`，页面与媒体会以该前缀相对加载。对外 URL 只影响链接，不改变监听接口。已设置对外 URL 时端口占用会启动失败，避免自动换端口导致代理失配；未设置时仍保留顺延最多 20 个端口的兼容行为。

独立画布也读取同一配置：`.venv/bin/python -m film.workflow_canvas <项目ID> --serve`。

---

## 五、验证是否装好

```bash
# 1. 确认 venv 里的 Python 版本在 3.10~3.13
.venv/bin/python --version

# 2. 确认关键依赖能导入
.venv/bin/python -c "import requests, bottle, aiohttp, mistune, bleach, prompt_toolkit; print('deps ok')"

# 3. 确认 ffmpeg 可用（imageio-ffmpeg 自带）
.venv/bin/python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"

# 4. 启动（能看到 Studio 链接即成功）
.venv/bin/python agentmain.py --no-studio
```

---

## 六、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 启动报 `[ERROR] config not found` | 没有 `vibefilming.config.json`，`cp vibefilming.config.example.json vibefilming.config.json` 后填 Key |
| 启动报 `BAD Mixin config` | 配置文件 JSON 格式错误或 `ark` 段缺字段，对照第三节检查 |
| 调用报 401 / 403 | `ark.api_key` 没填对，或该模型没在方舟开通访问权限 |
| `python3` 跑起来还是系统 Python | shell 有 alias，改用 `.venv/bin/python` |
| 端口 8764 被占用 | 用 `--studio-port` 换端口 |
| BGM 报 200028 APINoSource | 火山 BigMusic 未开通，去火山控制台开「音乐生成」，或不填 `volc` 跳过 BGM |
| ffmpeg not found | 一般不会；如报错 `brew install ffmpeg`（macOS）/ `apt install ffmpeg`（Linux） |
| 视频任务长时间不返回 | 视频模型单段 200–300s 正常，查询最多阻塞 5 分钟，超时可重试 |

---

## 七、产物在哪

每个项目的状态与产物都在 `projects/<project_id>/` 下，进程被 kill 也不丢：

```txt
projects/<project_id>/
├── manifest.json          ← 项目状态（阶段/素材/预算）
├── director_plan.json     ← 编导方案（段落/资产/成片，单一事实源）
├── canvas.html            ← 本地可视化工作流看板
├── entities/              ← 角色/道具/场景基准图
├── shots/                 ← 每个镜头的 mp4 + 关键帧
├── composed/              ← final_xxx.mp4（最终交付）
├── audios/                ← BGM
├── reviews/               ← 每次 AI 审片记录
└── logs/model_calls.jsonl ← 全量模型调用日志（含 token / cache 用量）
```
