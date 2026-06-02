# Skill Search — 105K 技能卡检索

> 从 105K+ 技能卡中语义搜索最匹配的 skill。零依赖，内置默认 API 地址，开箱即用。

## 最简调用

```python
import sys; sys.path.append('../skills/skill_search')
from skill_search import search

results = search("python send email")  # ⚠️ 必须用英文查询，中文匹配效果极差
for r in results:
    s = r.skill
    print(f"[{r.final_score:.2f}] {s.name} — {s.one_line_summary}")
    print(f"  key: {s.key}  category: {s.category}  tags: {s.tags[:3]}")
```

## API 签名

```python
search(query, env=None, category=None, top_k=10) -> list[SearchResult]
#  env: 自动检测，一般不传
#  category: 可选过滤，如 "devops"
#  top_k: 返回数量，默认10
```

## 返回结构

```
SearchResult
  .final_score    float     综合评分 (0~1)
  .relevance      float     语义相关度
  .quality        float     质量分
  .match_reasons  list[str] 匹配原因
  .warnings       list[str] 警告
  .skill          SkillIndex ↓

SkillIndex (常用字段)
  .key              str       唯一标识/路径
  .name             str       名称
  .one_line_summary str       一句话摘要
  .description      str       详细描述
  .category         str       类别
  .tags             list[str] 标签
  .form             str       形式(sop/script/...)
  .autonomous_safe  bool      是否自主安全
```

## CLI

```bash
python -m skill_search "python testing"
python -m skill_search "docker deployment" --category devops --top 5
python -m skill_search "git" --json
python -m skill_search --stats
python -m skill_search --env
```

## 配置

| 项 | 默认值 | 说明 |
|---|---|---|
| API地址 | `http://www.fudankw.cn:58787` | 环境变量 `SKILL_SEARCH_API` 可覆盖 |
| API密钥 | 无(可选) | 环境变量 `SKILL_SEARCH_KEY` |