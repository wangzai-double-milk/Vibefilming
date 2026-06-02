"""skill_search — Skill 检索 API 客户端"""
from .engine import (
    SkillIndex, SearchResult, SkillSearchError,
    search, get_stats, detect_environment,
)

__all__ = ["SkillIndex", "SearchResult", "SkillSearchError",
           "search", "get_stats", "detect_environment"]