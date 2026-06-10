# 怎么加一个新的 film 工具

## 先想清楚：要不要加工具？

加工具不是默认动作。**大多数新能力应该写成 skill（知识），而不是 tool（代码）**。判断标准：

**需要加工具（注册 @film_tool）** —— 只有当你要引入一个**全新的底层原子动作**，即一次 agent 无法用现有工具组合完成的、确定性的 API/ffmpeg 调用。典型信号：
- 要调一个**现有工具都没碰过的外部接口**（新的生成模型、新的云服务）；
- 要做一个**现有 ffmpeg 工具组合不出来的视频/音频处理原语**；
- 这个动作是**确定性的、可复用的、参数化的**，不依赖"怎么编排"的判断。

**不需要加工具（写 skill 就够）** —— 如果你想表达的是「**流程、规范、套路、判断标准、prompt 模板**」，那是知识，应该写进 `skills/<name>/SKILL.md`，让 agent 按需 `file_read`，**不要**塞进工具：
- "做某类视频该走什么步骤" → skill（如 storyboard / film_production）；
- "什么样的画面算过审 / 怎么问 VLM" → skill（如 director_vlm）；
- "prompt 怎么写才合规" → skill（如 prompt_engineering）；
- 只是把几个现有工具**按顺序组合**起来 → 不是新工具，是 skill 里的编排说明。

> 一句话：**工具 = 新的"手"（原子动作），skill = 新的"脑"（怎么用手）**。
> 能用现有工具组合 + 一段说明搞定的，永远优先写 skill。这也是为什么旧的
> `vibefilming_shortvideo_sop.md` 被拆成了 `skills/` 下的多个 SKILL.md——流程是知识，不是工具。

---

## 加工具：只需要一处改动

确认确实要加工具后，现在只需要**一处**改动：在 `film/tools.py` 里写一个函数，挂上 `@film_tool` 装饰器。
装饰器会自动帮你做两件事：

1. **自动注册** —— 工具会被挂到 handler 上（`do_<name>`），agent 就能调它。
2. **自动生成 schema** —— 不用再去 `assets/tools_schema_film.json` 手写 JSON 定义，
   装饰器会按你声明的 `params` 自动拼出 OpenAI 风格的 function schema 并并入模型可见的工具表。

---

## 最小例子

**写在哪**：打开 `film/tools.py`，找一个跟你工具同类的区块（文件里用注释分了
`# === 视觉生成 ===` / `# === 视频处理 ===` / `# === 音频 ===` / `# === 评估归档 ===` 等几段），
把下面这段函数贴进去就行。位置不挑——只要在 `film_tool` 定义之后、文件末尾
`inject_film_tools(...)` 之前的任意地方都可以。

假设要加一个语音合成工具 `my_tts`，贴进 `film/tools.py` 的「音频」区块：

```python
@film_tool(
    name="my_tts",
    desc="把文字合成语音，落到 composed/<name>.mp3",
    params={
        "text": str,                                    # 必填、纯字符串
        "name": str,
        "voice": {"type": str, "default": "default"},   # 选填、带默认值
    },
    required=["text", "name"],
)
def _my_tts(handler, args):
    save = _project_path(handler, "composed", f"{args.get('name', 'tts')}.mp3")
    return sdk.my_tts(args["text"], save, voice=args.get("voice", "default"))
```

写完就行。**不用**再动 `TOOL_REGISTRY`，**不用**再动 `tools_schema_film.json`。

---

## 函数约定

每个工具函数签名固定是 `def _xxx(handler, args):`

- `handler`：当前 agent handler 实例。拿活跃项目用 `_active_pid(handler)`，
  拼项目内路径用 `_project_path(handler, "子目录", "文件名")`。
- `args`：dict，模型传进来的参数（已自动去掉以 `_` 开头的内部字段）。
- **返回值**：返回一个 `dict` 即可。包装层 `_wrap` 会自动：
  - 打印进度（`🎬 工具名(...)` / `✅ 结果`）
  - 写日志 `ws.log_tool_call(...)` 到项目的 `tool_calls.jsonl`
  - 包成 `StepOutcome` 交回 agent loop
  - 异常自动兜底成 `{"status": "error", ...}`，**所以函数里直接 `raise` 报错就行**，不用自己 try/except。

实际的 HTTP / ffmpeg 调用放在 `film/film_sdk.py`，工具函数只做「取参数 → 算路径 → 调 sdk → 返回」这层薄包装。

---

## params 怎么写

`params` 是 `{参数名: 规格}`，规格支持两种写法：

### 1. 直接写 Python 类型（最简）

```python
params={"text": str, "count": int, "ratio": float, "flag": bool,
        "urls": list, "config": dict}
```

类型会自动映射成 JSON schema 类型：

| Python | JSON      |
|--------|-----------|
| `str`  | `string`  |
| `int`  | `integer` |
| `float`| `number`  |
| `bool` | `boolean` |
| `list` | `array`   |
| `dict` | `object`  |

### 2. 写成 dict（需要 description / default / enum / 嵌套时）

```python
params={
    "size":   {"type": str, "description": "尺寸", "default": "1024x1024"},
    "mode":   {"type": str, "enum": ["auto", "video", "frames"], "default": "auto"},
    "fps":    {"type": float, "default": 1.0},
    # 数组元素类型 / 嵌套 object 直接写 JSON 形式，原样透传：
    "urls":   {"type": "array", "items": {"type": "string"}, "description": "URL 列表"},
}
```

dict 里的 `type` 字段既可以写 Python 类型（`str`/`int`/...，会被自动映射），
也可以直接写 JSON 字符串（`"array"`/`"object"`/...，原样透传）。
其余字段（`description`/`default`/`enum`/`minimum`/`maximum`/`items` 等）**原样保留**，
所以复杂参数（带枚举、带范围、带嵌套结构）也能完整描述。

> 特殊情况：某个参数想**不带 type**（允许多种类型，比如既可传字符串也可传数组），
> 就在 dict 里**不写 `type`** 即可：
> `"some_arg": {"description": "路径(str) 或路径列表(array)"}`
> 不过更推荐按语义拆成独立字段（如视频用 `video`、图片列表用 `images`），
> 类型清晰、log 好查，避免一个字段塞多种类型。

---

## required

`required=[...]` 列必填参数名。没列进去的就是选填，函数里用 `args.get("x", 默认值)` 取。

---

## 工具名的 [Film] 前缀

装饰器会自动给 `desc` 加上 `[Film] ` 前缀（区分通用 GA 工具）。
所以 `desc` 里**不要**自己再写 `[Film]`。

---

## 想加别名（一个函数两个名字）

正常一个函数一个 `@film_tool` 名。如果想让旧名也能调到同一个函数（只路由、不向模型暴露），
在函数定义后手动登记进 `_DECORATED_TOOLS` 即可，参考现有的 `gen_bgm`：

```python
# 别名兜底：旧名 gen_bgm 也路由到 gen_audio_bgm，但不进 schema（不向模型暴露旧名）
_DECORATED_TOOLS["gen_bgm"] = _gen_audio_bgm
```

---

## 验证

加完工具，跑一下确认注册和 schema 都 ok：

```bash
python3 -c "
import film.tools as t
class H: pass
h = H(); t.inject_film_tools(h)
print('总工具数:', len({**t.TOOL_REGISTRY, **t._DECORATED_TOOLS}))
print('我的新工具已绑定:', hasattr(h, 'do_my_tts'))
print('schema 里有它:', any(s['function']['name']=='my_tts' for s in t.build_film_schema()))
"
```

三行都 ok 就说明工具加成功了。
