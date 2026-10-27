# Reference Video Observation Contract

Analyze the supplied video directly. Return one valid JSON object only, without a
Markdown fence. Do not call tools, read project files, design a remake, or infer
events outside the visible and audible evidence.

## Observation rules

1. Cover the full probed duration. The first segment starts at `00:00`, adjacent
   segments touch exactly, and the final segment reaches the supplied duration.
2. Split on real cuts or meaningful state changes inside a continuous shot. Do
   not invent cuts to fit a downstream model limit.
3. Separate observation from interpretation. Mark structural purpose and target
   audience as inference. Unknown numbers, text, audio, timing, or off-screen
   state remain unknown.
4. Preserve source identity facts independently: medium, realism, character
   proportions, materials, lighting, camera, environment, objects, HUD, text,
   sound, event order, contacts, quantities, and end states.
5. For motion, record start state, path, direction, speed change, contact or
   force, feedback, and stable result. Do not collapse a causal chain into a
   generic verb such as "attack", "collect", or "upgrade".
6. Distinguish character or object motion, world scrolling, camera motion, and
   screen-space UI animation.
7. Distinguish decorative effects from gameplay feedback. Record trigger,
   position, shape, color, direction, duration, and visible state change.
8. For games, record input or automatic trigger, action, propagation/contact,
   visual and audio feedback, resource/object state after, and next goal.
9. For pacing, estimate first effective event, action duration, repeated-event
   interval, feedback density, and largest inactive window only at the precision
   supported by the supplied sampling. Four fps does not justify 0.1-second
   claims.
10. Transcribe only legible on-screen text and intelligible speech. Do not infer
    audio from the image.
11. List uncertainties explicitly. A single continuous shot may be one segment.

## Output schema

```json
{
  "segments": [
    {
      "shot": "分镜1",
      "shot_size": "近景",
      "camera": "固定",
      "content": "可见场面、对象、动作、接触、反馈和稳定结果",
      "shooting": "媒介、空间、光照、连续性和真实切镜信息",
      "onscreen_text": "只转写可辨文字；不可辨填 -",
      "dialogue": "可辨台词；不可辨填 -",
      "audio": "有音频证据才填写，否则 -",
      "start": "00:00",
      "end": "00:04.25",
      "duration": "00:04.25",
      "shot_info": {
        "shotAngle": "平视",
        "shotSize": "近景",
        "shotCameraMovement": "固定",
        "expressionStyle": "可见表达方式",
        "expressionPurpose": "推断：本镜在结构中的作用"
      }
    }
  ],
  "script_summary": {
    "scriptStructureFormula": "只基于可见全片结构",
    "scriptHookForm": "开场如何引起注意",
    "scriptMainScenarios": "实际出现的场景",
    "scriptUserPainPoints": "明确表达才填写，否则未知",
    "scriptProductSellingPoints": "区分广告表达与已核准事实",
    "scriptTargetAudience": "有依据则注明推断，否则未知"
  },
  "uncertainties": [
    "无法确认的时间范围、对象、动作、数字、文字或声音，以及原因"
  ]
}
```

Use `MM:SS` or `MM:SS.sss`. Preserve decimals; each duration equals end minus
start. Use `-` for empty text. Examples in this contract are schema examples,
not facts about the supplied video.
