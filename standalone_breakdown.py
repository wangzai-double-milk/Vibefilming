#!/usr/bin/env python3
"""独立拆镜脚本（从 film/reference_breakdown.py 摘出的可单独运行版本）。

只做一件事：输入一个参考视频（本地路径或 http/https URL），用豆包 VLM
逆向拆成 Markdown 分镜表 + 结构化 JSON，并跑两级 LLM 分析（逐镜 5 字段 +
全局 6 字段），产物写到视频旁边的 <stem>_breakdown/ 目录。

与整套 agent 框架解耦：
  - 不 import film.* / workspace / tools，不依赖活跃项目、manifest、画布；
  - 纯 Python 标准库（urllib 直调火山方舟），零第三方依赖；
  - API Key 三处读取：--api-key > 环境变量 ARK_API_KEY > vibefilming.config.json 的 ark.api_key；
  - 阿里 Qwen 兜底可选：配置 DASHSCOPE_API_KEY 后，豆包失败会自动切 qwen-vl-max-latest。

用法：
  python3 standalone_breakdown.py <视频路径或URL> [选项]

示例：
  python3 standalone_breakdown.py ./my_ref.mp4
  python3 standalone_breakdown.py ./my_ref.mp4 --mode imitate --fps 0.5
  python3 standalone_breakdown.py "https://example.com/ref.mp4" --out-dir ./out

选项：
  --mode    拆镜后写剧本模式：imitate(仿写) / fission(裂变)。仅写入产物元信息，默认留空 pending。
  --fps     视频抽帧速率（大视频调低省时），默认 1.0。
  --out-dir 产物输出目录，默认 <视频所在目录>/<视频名>_breakdown/。
  --api-key 火山方舟 Ark API Key（覆盖配置文件）。
  --no-analyze  跳过两级 LLM 分析（只出拆镜表，更快）。
  --force  已有拆镜产物时强制重拆。

产物（都写到 --out-dir）：
  storyboard_from_reference.md  VLM 原始 Markdown 分镜表（人类可读）
  reference_breakdown.json      解析后的结构化 segments + 两级分析结果
  script.md                     拆镜表草稿
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# 火山方舟 API 常量（与 film/film_sdk.py 默认一致，可被配置文件覆盖）
# --------------------------------------------------------------------------
DEFAULT_ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_VLM_MODEL = "doubao-seed-2-1-pro-260628"

QWEN_VL_MODEL = "qwen-vl-max-latest"
QWEN_VL_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
QWEN_INLINE_MAX_BYTES = 100 * 1024 * 1024

BREAKDOWN_JSON_FILENAME = "reference_breakdown.json"
BREAKDOWN_MD_FILENAME = "storyboard_from_reference.md"


class BreakdownParseError(Exception):
    """拆镜结果解析/校验失败（供兜底或直接报错）。"""


def _source_identity(video: str) -> dict:
    if video.startswith(("http://", "https://")):
        return {"kind": "url", "url": video}
    path = Path(video).resolve()
    stat = path.stat()
    return {
        "kind": "local",
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _request_signature(video: str, mode: str, fps: float, no_analyze: bool) -> str:
    payload = {
        "source": _source_identity(video),
        "mode": mode or "pending",
        "fps": float(fps),
        "no_analyze": bool(no_analyze),
        "model": _vlm_model(),
        "prompt": BREAKDOWN_PROMPT,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


# ============== 拆镜 Prompt（与 film/reference_breakdown.py 完全一致） ==============
BREAKDOWN_PROMPT = """# 角色
你是一位拥有丰富经验的短视频拆解专家及分镜导演。你具备敏锐的观察力，能从成片中反向推导出拍摄时的镜头语言、光影布局和具体的实操技法。

# 任务
逐帧分析我提供的参考视频，把它**完整**逆向拆解为一份可执行的分镜拍摄脚本，并严格按照下方的 Markdown 表格格式输出。

# 核心约束（必须严格遵守）
1. 空值填充：某字段在当前镜头中没有内容时，输出符号「-」，严禁留空或使用「无 / None / N/A」等文字。
2. 时间连续性：上一镜的「结束时间」必须**严格等于**下一镜的「开始时间」；第一个镜头的开始时间必须是 00:00；最后一个镜头的结束时间必须等于视频总时长；时间格式统一使用 MM:SS（不足 10 秒补零，如 00:03）。
3. 内容衔接：相邻镜头的「画面内容」必须存在逻辑连贯性；「口播/台词」语义完整，跨镜头的句子在结尾用「...」表示未完。
4. 格式纯净：仅输出 Markdown 表格，不要包含任何多余的解释、总结、开场白或结束语。
5. 表格结构：
   - 第一行必须是表头行，列名与「字段要求」中的列定义逐一对应且顺序一致（后端按这一行动态解析，缺失表头行或改列名会导致解析失败）；
   - 第二行必须是分隔线，格式为「| :--- | :--- | ...」，且分隔线的列数与表头行完全一致；
   - 每一行数据行的列数必须与表头行完全一致，空列也要保留占位（填「-」）。
6. 单元格内容：单元格内严禁出现竖线「|」字符（会破坏列切分），需要分行时统一使用「<br>」；表格结束后不要再追加任何文字。

# 字段要求
| 字段 | 要求 |
|---|---|
| 分镜 | 镜头顺序，数字递增，从 1 开始，输出「分镜1」「分镜2」……「分镜N」 |
| 景别 | 远景 / 全景 / 中景 / 近景 / 特写 / 微距 等标准景别术语 |
| 运镜 | 推 / 拉 / 摇 / 移 / 跟 / 升降 / 静止 / 甩镜头 等，可补速度与机位信息 |
| 画面内容 | 客观描述画面中发生的核心动作和主体，不做主观评价 |
| 拍摄指导 | **核心字段**。根据画面效果反推"如何复刻这个镜头"：机位高度（如低角度仰拍）、持机方式（如手持跟随 / 三脚架固定）、对焦与光线（如锁定高光、侧逆光拍摄）、特殊技巧（如手机倒置贴地） |
| 画面文案/花字 | **核心字段**。提取视频画面中出现的非语音文字信息（排除 CC 字幕） |
| 口播/台词 | 记录该镜头对应的语音逐字稿 |
| 音效/BGM | 听到的背景音乐情绪或具体的环境音效 |
| 开始时间 | 镜头开始时间点，格式 MM:SS |
| 结束时间 | 镜头结束时间点，格式 MM:SS |
| 时长 | 镜头持续时间，格式 MM:SS |

# 输出格式示例
| 分镜 | 景别 | 运镜 | 画面内容 | 拍摄指导 | 画面文案/花字 | 口播/台词 | 音效/BGM | 开始时间 | 结束时间 | 时长 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 分镜1 | 特写 | 缓慢前推 | 晨光透过纱帘，照射在木质桌面的花瓶上 | 利用侧逆光方向，降低手机曝光(-0.5EV)以保留光束质感，手持手机缓慢平稳靠近主体 | 封面标题：《独居生活》<br>副标：Morning Routine | - | 轻快吉他曲+清晨鸟鸣 | 00:00 | 00:03 | 00:03 |
| 分镜2 | 全景 | 固定镜头 | 博主穿着家居服从卧室走出，伸了个懒腰 | 使用三脚架固定机位，高度约腰部位置（平视），保持画面横平竖直，利用门框作为前景构图 | 花字：又是元气满满的一天 | 大家都问我怎么保持早起... | 脚步声 | 00:03 | 00:06 | 00:03 |
| 分镜3 | 近景 | 跟随摇摄 | 手部特写，正在手冲咖啡，水流注入粉层 | 双手持机，手肘抵住桌面做支点，身体随手部动作轻微晃动，营造第一人称代入感 | 贴纸：#CoffeeTime | ...其实这只需要一个小习惯。 | 倒水声+轻微气泡声 | 00:06 | 00:10 | 00:04 |

# 执行流程
1. 读取视频：获取视频总时长，确认分析范围。
2. 识别切点：逐帧分析，识别镜头切换点（画面突变、场景转换、视角变化等）。
3. 提取信息：为每个镜头提取景别、运镜、画面内容、拍摄指导、文案、台词、音效等要素。
4. 校验时间：确保所有时间点首尾相连，第一镜开始=00:00，最后一镜结束=视频总时长。
5. 填充空值：检查所有字段，空缺内容统一使用「-」填充。
6. 输出表格：先输出表头行与分隔线，再逐行输出数据行，仅输出 Markdown 表格。

如条件允许，请在表格结束后另起一段输出 ```<!-- JSON:[...] -->``` 注释块（数组元素字段与表格列同名同顺序），便于后端结构化解析。此为可选。"""


# ============== 列名归一 ==============
_FIELD_ALIASES = {
    "shot": ("分镜", "镜号", "镜头", "shot"),
    "shot_size": ("景别", "shot_size"),
    "camera": ("运镜", "camera"),
    "content": ("画面内容", "content"),
    "shooting": ("拍摄指导", "shooting"),
    "onscreen_text": ("画面文案/花字", "画面文案", "花字", "onscreen_text"),
    "dialogue": ("口播/台词", "口播", "台词", "dialogue"),
    "audio": ("音效/BGM", "音效", "BGM", "audio"),
    "start": ("开始时间", "start"),
    "end": ("结束时间", "end"),
    "duration": ("时长", "duration"),
}


def _field(seg: dict, key: str) -> str:
    for alias in _FIELD_ALIASES.get(key, (key,)):
        if alias in seg and seg[alias] is not None:
            value = str(seg[alias]).strip()
            if value and value not in ("-", "—", "无", "N/A"):
                return value
    return ""


def _to_seconds(value) -> Optional[float]:
    text = str(value or "").strip().replace("，", "").replace(",", "")
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        total = 0.0
        for n in nums:
            total = total * 60 + n
        return total
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def _norm_time(value: str) -> str:
    secs = _to_seconds(value)
    if secs is None:
        return str(value).strip()
    secs = int(round(secs))
    if secs >= 3600:
        return f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
    return f"{secs // 60:02d}:{secs % 60:02d}"


# ============== 两级 LLM 分析 Prompt（移植自 med-ai-factory 拆解脚本） ==============
SHOT_INFO_SYSTEM_PROMPT = """你是专业的广告视频分镜分析师。会给你一个视频分镜的以下信息：
- camera: 机位描述（如"先特写近脸部，随后切中景跟拍人物动作"）
- action_description: 动作描述
- voice_instruction: 配音指令（含 prosody 语气/语调 和 state 状态）

你的任务是从这些信息中提取以下 5 个字段：

【字段定义】
- shotAngle: 拍摄角度。取值范围：平视 / 仰拍 / 俯拍
- shotSize: 景别。取值范围：特写 / 近景 / 中景 / 全景 / 远景
- shotCameraMovement: 运镜。取值范围：固定 / 推 / 拉 / 摇 / 移 / 跟
- expressionStyle: 表达方式。需要细分，参考以下示例但不限于：
  亲切真实分享、直观场景展示、自然场景展示、专业技术讲解
  亲切体验分享、共情体验分享、专业权威讲解、专业真实分享
  场景化实用展示、情感调动、痛点引入、对比展示、用户证言
  根据画面内容和配音语气选择最贴切的一个细分标签。
- expressionPurpose: 表达目的。可以有多个，用逗号分隔。参考以下但不限于：
  痛托气氛、产品功效、营销卖点、引导下单、痛点共鸣
  品牌信任、使用场景、技术背书、情感连接、行动号召

【规则】
1. 如果 camera 描述中有多个镜头变化，取主要的一个。
2. 如果信息不足以判断，根据 action_description 和 voice_instruction 推断。
3. expressionPurpose 可以返回 1-3 个目的，用逗号分隔，如"产品功效,营销卖点,引导下单"。
4. expressionStyle 只返回一个最贴切的细分标签。
5. 只输出 JSON 对象，不要输出任何额外解释，不要 Markdown 代码块标记。

【输出格式】
{"shotAngle":"平视","shotSize":"中景","shotCameraMovement":"固定","expressionStyle":"专业技术讲解","expressionPurpose":"产品功效,技术背书"}"""

SCRIPT_SUMMARY_SYSTEM_PROMPT = """你是专业的广告视频脚本分析师。会给你一个视频所有分镜的结构化信息，每个分镜包含：
- actionDescription: 画面动作描述
- dialogue: 对话文本
- expressionPurpose: 表达目的（如：痛点托气氛/产品功效,营销卖点,引导下单）
- expressionStyle: 表达方式（如：专业技术讲解/亲切体验分享）

你的任务是对整个视频做全局脚本分析，提取以下 6 个字段：

【字段定义】
- scriptStructureFormula: 结构公式。把所有分镜的表达目的按顺序用斜线连接，相同的需要去重
- scriptHookForm: 钩子形式。分析开头分镜的吸引手法，相同的需要去重
- scriptMainScenarios: 主打场景。多个用顿号分隔，相同的需要去重
- scriptUserPainPoints: 用户痛点。多个用顿号分隔，相同的需要去重
- scriptProductSellingPoints: 产品卖点。多条用分号分隔，相同的需要去重
- scriptTargetAudience: 圈定人群。多个用顿号分隔，相同的需要去重

【规则】
1. scriptStructureFormula 只看每个分镜的 expressionPurpose 的第一个目的，按分镜顺序用斜线连接。
2. 其余字段从所有分镜中汇总提取，去重后用分隔符连接。
3. 只输出 JSON 对象，不要输出任何额外解释。

【输出格式】
{"scriptStructureFormula":"痛点托气氛-营销卖点-产品功效-引导下单","scriptHookForm":"情绪调动型+惊喜感","scriptMainScenarios":"外出时随时按摸、日常肩颈酸痛时使用","scriptUserPainPoints":"肩颈酸累、僵冷疲劳","scriptProductSellingPoints":"轻巧精干，新增折叠式设计；搭载混动按摸技术","scriptTargetAudience":"有肩颈舒缓需求的女性"}"""

SHOT_INFO_KEYS = ("shotAngle", "shotSize", "shotCameraMovement", "expressionStyle", "expressionPurpose")


# ============== 配置 & HTTP ==============
def _load_config() -> dict:
    candidates = [
        Path(__file__).resolve().parent / "vibefilming.config.json",
        Path.cwd() / "vibefilming.config.json",
    ]
    for p in candidates:
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


_CONFIG = _load_config()


def _config_get(*path, default=None):
    cur = _CONFIG
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _resolve_ark_key(cli_key: str = "") -> str:
    if cli_key:
        return cli_key
    env = os.environ.get("ARK_API_KEY", "").strip()
    if env:
        return env
    return (_config_get("ark", "api_key", default="") or "").strip()


def _ark_base() -> str:
    return _config_get("ark", "api_base", default="") or DEFAULT_ARK_BASE


def _vlm_model() -> str:
    return _config_get("ark", "models", "vlm", default="") or DEFAULT_VLM_MODEL


def _text_model() -> str:
    return _config_get("ark", "models", "text", default="") or "deepseek-v4-pro-260425"


def _http_post(url: str, body: dict, api_key: str, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code} on {url}: {body_text[:300]}")


def _extract_content(raw: dict) -> str:
    try:
        content = (raw["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise RuntimeError(f"VLM 返回结构异常：{json.dumps(raw, ensure_ascii=False, default=str)[:300]}")
    if not content:
        raise RuntimeError("VLM 返回 content 为空")
    return content


# ============== VLM：豆包主路径 + 阿里兜底 ==============
def _doubao_video_understand(video_path: str, text: str, api_key: str, fps: float) -> str:
    if video_path.startswith(("http://", "https://")):
        video_url = video_path
    else:
        p = Path(video_path)
        if not p.is_file():
            raise FileNotFoundError(f"视频不存在：{p}")
        b64 = base64.b64encode(p.read_bytes()).decode()
        suffix = p.suffix.lower().lstrip(".") or "mp4"
        video_url = f"data:video/{suffix};base64,{b64}"

    body = {
        "model": _vlm_model(),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "video_url", "video_url": {"url": video_url, "fps": fps}},
                {"type": "text", "text": text},
            ],
        }],
        "max_tokens": 16384,
        "temperature": 0.1,
    }
    raw = _http_post(f"{_ark_base()}/chat/completions", body, api_key, timeout=1800)
    return _extract_content(raw)


def _doubao_chat(messages: list, api_key: str, max_tokens: int) -> str:
    body = {
        "model": _text_model(),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    raw = _http_post(f"{_ark_base()}/chat/completions", body, api_key, timeout=600)
    return _extract_content(raw)


def _qwen_vl_fallback(video_path: str, prompt: str) -> str:
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY 环境变量，阿里 VLM 兜底不可用")

    if video_path.startswith(("http://", "https://")):
        video_url = video_path
    else:
        p = Path(video_path)
        if not p.is_file():
            raise FileNotFoundError(f"参考视频不存在：{p}")
        size = p.stat().st_size
        if size > QWEN_INLINE_MAX_BYTES:
            raise RuntimeError(
                f"本地视频 {size / 1024 / 1024:.1f}MB 超过兜底内嵌上限 "
                f"{QWEN_INLINE_MAX_BYTES // 1024 // 1024}MB：请提供 URL 或压缩视频"
            )
        suffix = p.suffix.lower().lstrip(".") or "mp4"
        video_url = f"data:video/{suffix};base64,{base64.b64encode(p.read_bytes()).decode()}"

    body = {
        "model": QWEN_VL_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "video_url", "video_url": {"url": video_url}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    raw = _http_post(QWEN_VL_ENDPOINT, body, api_key, timeout=600)
    return _extract_content(raw)


def _vlm_breakdown(video_path: str, api_key: str, fps: float) -> tuple:
    try:
        return _doubao_video_understand(video_path, BREAKDOWN_PROMPT, api_key, fps), "doubao"
    except (RuntimeError, FileNotFoundError, TimeoutError) as e:
        doubao_err = e
    try:
        return _qwen_vl_fallback(video_path, BREAKDOWN_PROMPT), QWEN_VL_MODEL
    except Exception as fallback_err:
        raise RuntimeError(
            f"参考视频拆镜失败：豆包主路径异常（{doubao_err}）；"
            f"阿里 {QWEN_VL_MODEL} 兜底也失败（{fallback_err}）"
        )


# ============== 解析：JSON 注释块优先，回退 Markdown 表 ==============
_JSON_BLOCK_RE = re.compile(r"<!--\s*JSON:\s*(\[.*?\])\s*-->", re.DOTALL)
_TABLE_ROW_RE = re.compile(r"^\|.*\|$", re.MULTILINE)
_SEPARATOR_RE = re.compile(r"^\|[\s:\-|]+\|$")


def _parse_breakdown(raw_text: str) -> list:
    m = _JSON_BLOCK_RE.search(raw_text or "")
    if m:
        try:
            segments = json.loads(m.group(1))
            if isinstance(segments, list) and segments:
                _validate_segments([s for s in segments if isinstance(s, dict)])
                return [s for s in segments if isinstance(s, dict)]
        except (json.JSONDecodeError, BreakdownParseError):
            pass

    rows = [r for r in _TABLE_ROW_RE.findall(raw_text or "") if not _SEPARATOR_RE.match(r.strip())]
    if len(rows) < 2:
        raise BreakdownParseError(f"未从 VLM 输出里解析到 Markdown 分镜表（有效行 {len(rows)} 行）")
    header = _split_row(rows[0])
    segments = []
    for row in rows[1:]:
        cells = _split_row(row)
        if len(cells) != len(header):
            raise BreakdownParseError(f"表格列数不一致：表头 {len(header)} 列，数据行 {len(cells)} 列 → {row[:80]}")
        segments.append(dict(zip(header, cells)))
    _validate_segments(segments)
    return segments


def _split_row(row: str) -> list:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _validate_segments(segments: list):
    if len(segments) < 2:
        raise BreakdownParseError(f"拆镜结果只有 {len(segments)} 个镜头，少于 2 个，判定为拆镜失败")
    first_start = _norm_time(_field(segments[0], "start"))
    if first_start != "00:00":
        raise BreakdownParseError(f"首镜开始时间应为 00:00，实际为 {first_start!r}")
    for i in range(1, len(segments)):
        prev_end = _norm_time(_field(segments[i - 1], "end"))
        cur_start = _norm_time(_field(segments[i], "start"))
        if prev_end != cur_start:
            raise BreakdownParseError(
                f"第 {i} 与第 {i + 1} 镜时间不连续：上一镜结束 {prev_end!r} != 本镜开始 {cur_start!r}"
            )


# ============== 两级 LLM 分析 ==============
def _llm_json(system: str, user: str, api_key: str, max_tokens: int = 1000) -> dict:
    try:
        raw = _doubao_chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            api_key, max_tokens=max_tokens,
        )
        content = (raw or "").strip()
    except Exception:
        return {}
    if not content:
        return {}
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _analyze_shot_info(seg: dict, api_key: str) -> dict:
    camera = " / ".join(x for x in (_field(seg, "camera"), _field(seg, "shooting")) if x)
    user = (
        "请根据以下信息提取镜头角度、景别、运镜、表达方式和表达目的，只输出 JSON。\n\n"
        f"camera: {camera}\n"
        f"action_description: {_field(seg, 'content')}\n"
        f"voice_instruction: {_field(seg, 'dialogue')}"
    )
    info = _llm_json(SHOT_INFO_SYSTEM_PROMPT, user, api_key, max_tokens=500)
    if not info.get("shotSize"):
        return {}
    return {k: str(info[k]).strip() for k in SHOT_INFO_KEYS if info.get(k)}


def _analyze_script_summary(segments: list, api_key: str) -> dict:
    if not segments:
        return {}
    parts = []
    for i, seg in enumerate(segments):
        info = seg.get("shot_info") or {}
        if not info.get("shotSize"):
            return {}
        parts.append(
            f"【分镜{i}】\n"
            f"actionDescription: {_field(seg, 'content')}\n"
            f"dialogue: {_field(seg, 'dialogue')}\n"
            f"expressionPurpose: {info.get('expressionPurpose', '')}\n"
            f"expressionStyle: {info.get('expressionStyle', '')}\n"
        )
    user = "以下是视频所有分镜的结构化信息，请做全局脚本分析，只输出 JSON。\n\n" + "\n".join(parts)
    return _llm_json(SCRIPT_SUMMARY_SYSTEM_PROMPT, user, api_key, max_tokens=1000)


def _build_narration(segments: list) -> str:
    lines = []
    for seg in segments:
        text = _field(seg, "dialogue")
        if not text:
            continue
        start = _norm_time(_field(seg, "start"))
        if start:
            lines.append(f"[{start}] {text}")
    return "\n".join(lines)


# ============== script.md 草稿 ==============
def _write_script_md(out_dir: Path, segments: list, mode: str, script_summary: dict) -> Path:
    lines = [
        "---",
        "source: reference_video",
        f"mode: {mode or 'pending'}",
        "draft: true",
        f"generated_at: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"segments_count: {len(segments)}",
        "---",
        "",
        "# 分镜脚本（参考视频拆镜草稿）",
        "",
        "> 本文件是拆镜表的草稿版：正式剧本需按 mode（imitate=仿写 / fission=裂变）改写后落盘。",
        "",
    ]
    for idx, seg in enumerate(segments):
        start = _norm_time(_field(seg, "start")) or "-"
        end = _norm_time(_field(seg, "end")) or "-"
        lines.append(f"## 场景 {idx + 1}（{start} - {end}）")
        lines.append("")
        for label, key in (("景别", "shot_size"), ("运镜", "camera"), ("画面内容", "content"),
                           ("拍摄指导", "shooting"), ("口播/台词", "dialogue"),
                           ("音效/BGM", "audio"), ("画面文案/花字", "onscreen_text")):
            value = _field(seg, key)
            if value:
                lines.append(f"- {label}：{value}")
        info = seg.get("shot_info") or {}
        if info:
            lines.append(f"- 镜头信息：角度 {info.get('shotAngle', '-')} / 景别 {info.get('shotSize', '-')}"
                         f" / 运镜 {info.get('shotCameraMovement', '-')}")
            if info.get("expressionStyle") or info.get("expressionPurpose"):
                lines.append(f"- 表达：{info.get('expressionStyle', '-')}"
                             f"（目的：{info.get('expressionPurpose', '-')}）")
        lines.append("")

    if script_summary:
        lines.append("## 全局脚本总结（两级分析第二级）")
        lines.append("")
        for label, key in (("结构公式", "scriptStructureFormula"), ("钩子形式", "scriptHookForm"),
                           ("主打场景", "scriptMainScenarios"), ("用户痛点", "scriptUserPainPoints"),
                           ("产品卖点", "scriptProductSellingPoints"), ("圈定人群", "scriptTargetAudience")):
            value = (script_summary or {}).get(key)
            if value:
                lines.append(f"- {label}：{value}")
        lines.append("")

    script_path = out_dir / "script.md"
    script_path.write_text("\n".join(lines), encoding="utf-8")
    return script_path


# ============== 主流程 ==============
def main(argv=None):
    ap = argparse.ArgumentParser(description="独立拆镜脚本：输入参考视频，输出分镜表 + 两级 LLM 分析")
    ap.add_argument("video", help="参考视频本地路径或 http/https URL")
    ap.add_argument("--mode", default="", choices=["", "imitate", "fission"],
                    help="写剧本模式：imitate=仿写 / fission=裂变（默认留空 pending）")
    ap.add_argument("--fps", type=float, default=1.0, help="视频抽帧速率（默认 1.0，大视频调低省时）")
    ap.add_argument("--out-dir", default="", help="产物输出目录，默认 <视频所在目录>/<视频名>_breakdown/")
    ap.add_argument("--api-key", default="", help="火山方舟 Ark API Key（覆盖配置文件/环境变量）")
    ap.add_argument("--no-analyze", action="store_true", help="跳过两级 LLM 分析，只出拆镜表")
    ap.add_argument("--force", action="store_true", help="已有拆镜产物时强制重拆")
    args = ap.parse_args(argv)

    api_key = _resolve_ark_key(args.api_key)
    if not api_key:
        print("错误：未找到 Ark API Key。请用 --api-key、环境变量 ARK_API_KEY 或 "
              "vibefilming.config.json 的 ark.api_key 提供。", file=sys.stderr)
        return 2

    video = args.video.strip()
    if not video:
        print("错误：请提供视频路径或 URL。", file=sys.stderr)
        return 2
    if video.startswith(("http://", "https://")):
        video_name = urllib.parse.urlparse(video).path.split("/")[-1] or "video"
        default_out = Path.cwd() / f"{Path(video_name).stem}_breakdown"
    else:
        vp = Path(video)
        if not vp.is_file():
            print(f"错误：视频文件不存在：{vp}", file=sys.stderr)
            return 2
        vp = vp.resolve()
        video = str(vp)
        default_out = vp.parent / f"{vp.stem}_breakdown"

    out_dir = Path(args.out_dir) if args.out_dir else default_out
    out_dir.mkdir(parents=True, exist_ok=True)

    breakdown_json = out_dir / BREAKDOWN_JSON_FILENAME
    request_signature = _request_signature(
        video, args.mode, args.fps, args.no_analyze)
    if breakdown_json.is_file() and not args.force:
        cached = json.loads(breakdown_json.read_text(encoding="utf-8"))
        if cached.get("request_signature") == request_signature:
            print(f"[跳过] 来源与参数未变化，复用拆镜产物：{breakdown_json}")
            print(f"  segments: {cached.get('segments_count')}   provider: {cached.get('provider_used')}"
                  f"   总时长: {cached.get('total_duration')}s")
            return 0
        print(f"[缓存失效] 来源、模式、采样或模型规则已变化，将重新拆镜：{breakdown_json}")

    print("正在拆镜（单次可能耗时 2~8 分钟，请耐心等待）...")
    print(f"  视频: {video}")
    print(f"  模型: {_vlm_model()}（失败自动切 Qwen 兜底）   fps: {args.fps}")
    print(f"  输出目录: {out_dir}")
    sys.stdout.flush()

    timing = {}
    t0 = time.time()
    raw_text, provider_used = _vlm_breakdown(video, api_key, args.fps)
    timing["vlm_primary"] = round(time.time() - t0, 2)

    t1 = time.time()
    try:
        segments = _parse_breakdown(raw_text)
        timing["parse"] = round(time.time() - t1, 2)
    except BreakdownParseError as primary_err:
        if provider_used != "doubao":
            print(f"错误：{primary_err}", file=sys.stderr)
            return 1
        try:
            raw_text = _qwen_vl_fallback(video, BREAKDOWN_PROMPT)
            provider_used = QWEN_VL_MODEL
            segments = _parse_breakdown(raw_text)
        except Exception as fallback_err:
            print(f"错误：豆包返回内容解析不合规（{primary_err}）；"
                  f"阿里 {QWEN_VL_MODEL} 兜底也失败（{fallback_err}）", file=sys.stderr)
            return 1
        timing["parse"] = round(time.time() - t1, 2)

    total_duration = _to_seconds(_field(segments[-1], "end")) or 0

    script_summary = {}
    if not args.no_analyze:
        t2 = time.time()
        for seg in segments:
            try:
                seg["shot_info"] = _analyze_shot_info(seg, api_key)
            except Exception:
                seg["shot_info"] = {}
        timing["shot_info_total"] = round(time.time() - t2, 2)
        timing["shot_info_count"] = float(len(segments))

        t3 = time.time()
        try:
            script_summary = _analyze_script_summary(segments, api_key)
        except Exception:
            script_summary = {}
        timing["script_summary"] = round(time.time() - t3, 2)
    else:
        timing["shot_info_total"] = 0
        timing["shot_info_count"] = 0
        timing["script_summary"] = 0

    narration = _build_narration(segments)

    breakdown_json.write_text(json.dumps({
        "source": "reference_video",
        "source_video": video,
        "source_identity": _source_identity(video),
        "request_signature": request_signature,
        "provider_used": provider_used,
        "mode": args.mode or "pending",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "segments_count": len(segments),
        "total_duration": total_duration,
        "narration": narration,
        "script_summary": script_summary,
        "registries": {"characters": [], "props": [], "scenes": []},
        "timing": timing,
        "segments": segments,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / BREAKDOWN_MD_FILENAME).write_text(raw_text, encoding="utf-8")
    script_path = _write_script_md(out_dir, segments, args.mode, script_summary)

    print("\n拆镜完成 ✔")
    print(f"  provider: {provider_used}")
    print(f"  分镜数: {len(segments)}   总时长: {total_duration}s   mode: {args.mode or 'pending'}")
    print(f"  分镜表(MD):  {out_dir / BREAKDOWN_MD_FILENAME}")
    print(f"  结构化JSON:  {breakdown_json}")
    print(f"  剧本草稿:    {script_path}")
    print(f"  耗时: {json.dumps(timing, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
