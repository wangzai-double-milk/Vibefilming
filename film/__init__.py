"""VibeFilming：影视短片 agent 的领域能力模块。

模块结构：
  workspace.py            项目工作区 / manifest.json 读写
  film_sdk.py             所有外部 API 封装（Doubao / Seedream / Seedance / ffmpeg）+ 内置 stub
  tools.py                工具适配层：把 SDK 包装成影视工具并注入运行时 handler
  reference_breakdown.py  素材收录 + 参考视频 VLM 拆镜（豆包主 + 阿里兜底）

启用方式：
  python agentmain.py
"""
__all__ = [
    "workspace", "film_sdk", "tools", "reference_breakdown", "storage",
    "video_models",
]
