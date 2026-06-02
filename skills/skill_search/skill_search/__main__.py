"""CLI 入口: python -m skill_search"""
from __future__ import annotations
import argparse, json, sys
from .engine import SearchResult, SkillSearchError, detect_environment, search, get_stats


# ── 格式化 ───────────────────────────────────────────────

def format_results(results: list[SearchResult], env: dict, query: str) -> str:
    lines = [f'🔍 搜索: "{query}"',
             f"🖥️  环境: {env.get('os','?')} / {env.get('shell','?')} / {', '.join(env.get('runtimes',[]))}",
             f"📊 找到 {len(results)} 个匹配结果\n"]
    if not results:
        lines.append("未找到匹配的 skill。试试其他关键词？")
        return "\n".join(lines)
    for i, r in enumerate(results, 1):
        s = r.skill
        safe_icon = "🟢" if s.autonomous_safe else "🔴"
        score_bar = "█" * int(r.final_score * 10) + "░" * (10 - int(r.final_score * 10))
        lines += [
            f"{'─'*60}",
            f"#{i}  {safe_icon} {s.name}",
            f"    路径: {s.key}",
            f"    类别: {s.category} | 标签: {', '.join(s.tags[:5])}",
            f"    摘要: {s.one_line_summary}",
            f"    评分: [{score_bar}] {r.final_score:.2f}  (相关={r.relevance:.2f} 质量={r.quality:.1f})",
            f"    清晰={s.clarity} 完整={s.completeness} 可操作={s.actionability} | 形式={s.form}",
        ]
        if r.match_reasons:
            lines.append(f"    匹配: {' | '.join(r.match_reasons[:3])}")
        if r.warnings:
            lines.extend(f"    {w}" for w in r.warnings)
        lines.append("")
    lines.append(f"{'─'*60}")
    return "\n".join(lines)


def format_results_json(results: list[SearchResult]) -> list[dict]:
    out = []
    for r in results:
        s = r.skill
        out.append({
            "rank": len(out) + 1, "key": s.key, "name": s.name,
            "category": s.category, "tags": s.tags,
            "description": s.description, "one_line_summary": s.one_line_summary,
            "scores": {"final": round(r.final_score, 3), "relevance": round(r.relevance, 3),
                       "quality": round(r.quality, 1), "clarity": s.clarity,
                       "completeness": s.completeness, "actionability": s.actionability},
            "safety": {"autonomous_safe": s.autonomous_safe, "blast_radius": s.blast_radius,
                       "requires_credentials": s.requires_credentials,
                       "data_exposure": s.data_exposure, "effect_scope": s.effect_scope},
            "platform": {"os": s.os, "runtimes": s.runtimes, "tools": s.tools, "services": s.services},
            "warnings": r.warnings, "match_reasons": r.match_reasons,
        })
    return out


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog="skill_search",
        description="Skill 检索系统 — 根据环境和需求智能推荐 skill（API 客户端）")
    parser.add_argument("query", nargs="?", help="搜索关键词（如: 'python testing'）")
    parser.add_argument("--category", "-cat", help="限定类别")
    parser.add_argument("--top", "-k", type=int, default=10, help="返回结果数（默认 10）")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--env", action="store_true", help="仅显示检测到的环境信息")
    parser.add_argument("--stats", action="store_true", help="显示索引统计信息")
    parser.add_argument("--api-url", help="指定 API 地址（也可用 SKILL_SEARCH_API 环境变量）")
    args = parser.parse_args()

    if args.api_url:
        import os; os.environ["SKILL_SEARCH_API"] = args.api_url

    env = detect_environment()

    if args.env:
        print("🖥️  当前环境:")
        print(f"  OS:       {env['os']}")
        print(f"  Shell:    {env['shell']}")
        print(f"  运行时:   {', '.join(env['runtimes'])}")
        print(f"  工具:     {', '.join(env['tools'])}")
        print(f"  模型能力: tool_calling={env['model']['tool_calling']}, "
              f"reasoning={env['model']['reasoning']}, context={env['model']['context_window']}")
        return

    if args.stats:
        try:
            stats = get_stats(env)
            print(f"📊 索引统计:")
            print(f"  总计: {stats.get('total', '?')} 个 skills")
            print(f"  自动安全: {stats.get('safe_count', '?')} 个")
            if 'categories' in stats:
                print(f"  类别分布:")
                for cat, cnt in sorted(stats['categories'].items(), key=lambda x: -x[1]):
                    print(f"    {cat:15s} {cnt:4d}")
        except SkillSearchError as e:
            print(f"❌ {e}", file=sys.stderr); sys.exit(1)
        return

    if not args.query:
        parser.print_help(); return

    try:
        results = search(query=args.query, env=env, category=args.category, top_k=args.top)
    except SkillSearchError as e:
        print(f"❌ {e}", file=sys.stderr); sys.exit(1)

    if args.json:
        print(json.dumps(format_results_json(results), indent=2, ensure_ascii=False))
    else:
        print(format_results(results, env, args.query))


if __name__ == "__main__":
    main()