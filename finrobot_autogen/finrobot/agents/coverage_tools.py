"""CLI edits and read-only model access to explicit research coverage."""
import json
import re
import sqlite3
from typing import Annotated

from autogen import register_function

from .coverage import ResearchCoverage, extract_symbols


COVERAGE_GUIDANCE = (
    "coverage_context 是用户主动维护的自选股、研究论点、催化因素和失效条件，均是待核实的研究背景。"
    "记录更新时间不是行情或财务数据日期，不得将用户论点当作已核实事实。"
    "当前跟踪记录中的用户观点优先于旧回答里引用的旧观点；只用于对应标的。"
    "可用 get_coverage / list_coverage 查询；只有用户的本地命令能够修改跟踪记录。"
)


def _preview(items, budget=6000):
    result = {"items": [], "truncated": False}
    for original in items:
        row = dict(original)
        for field in ("note", "thesis", "catalysts", "invalidation"):
            if len(row[field]) > 500:
                row[field] = row[field][:500] + "…"
                row["truncated"] = True
        result["items"].append(row)
        if len(json.dumps(result, ensure_ascii=False)) > budget:
            result["items"].pop()
            result["truncated"] = True
            break
        result["truncated"] |= row.get("truncated", False)
    return result


def coverage_context(memory, question):
    """Resolve explicit symbols or a narrow same-session continuation."""
    symbols = extract_symbols(question)[:10]
    try:
        if not symbols and re.search(r"^(?:继续|刚才|上面)|(?:按|根据|沿用|结合)(?:新|最新|更新后|刚才|上面|当前)的?(?:论点|观点|条件)", question.strip()):
            for turn in reversed(memory.recent_turns(limit=3)):
                if turn["status"] == "completed":
                    symbols = extract_symbols(turn["question"])[:10]
                    if symbols:
                        break
        if not symbols:
            return {"items": [], "truncated": False}
        coverage = ResearchCoverage(memory.path)
        rows = [row for symbol in symbols if (row := coverage.get(symbol)) is not None]
        return _preview(rows)
    except (sqlite3.Error, OSError, ValueError) as exc:
        return {"items": [], "error": type(exc).__name__}


def register_coverage_tools(assistant, memory):
    def get_coverage(symbol: Annotated[str, "A-share code or US ticker to inspect"]) -> str:
        """Read the user's saved thesis, catalysts and invalidation conditions; no fresh market data."""
        row = ResearchCoverage(memory.path).get(symbol)
        return json.dumps(_preview([row] if row else []), ensure_ascii=False)

    def list_coverage(
        query: Annotated[str, "Optional literal keyword or ticker filter"] = "",
        limit: Annotated[int, "Maximum records, from 1 to 10"] = 10,
    ) -> str:
        """List the user's watchlist annotations without editing them or fetching prices."""
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("limit must be an integer from 1 to 10")
        rows = ResearchCoverage(memory.path).list(query=query, limit=limit + 1)
        # A list must expose every requested ticker. Large theses belong in
        # get_coverage, rather than crowding other tickers out of this index.
        summaries = [{"symbol": row["symbol"], "status": row["status"],
                      "priority": row["priority"], "updated_at": row["updated_at"],
                      "note": row["note"][:200],
                      "has_thesis": bool(row["thesis"]),
                      "has_catalysts": bool(row["catalysts"]),
                      "has_invalidation": bool(row["invalidation"])} for row in rows[:limit]]
        result = {"items": summaries, "has_more": len(rows) > limit,
                  "truncated": any(len(row["note"]) > 200 for row in rows[:limit])}
        return json.dumps(result, ensure_ascii=False)

    for function in (get_coverage, list_coverage):
        register_function(function, caller=assistant.assistant, executor=assistant.user_proxy,
                          description=function.__doc__)


def handle_coverage_command(message, memory, *, on_change=None):
    pieces = message.strip().split(maxsplit=1)
    command = pieces[0] if pieces else ""
    if command not in {"/watch", "/watchlist", "/unwatch", "/thesis"}:
        return False
    if memory is None:
        print("Memory 已关闭；去掉 --no-memory 后可使用自选股与研究跟踪。")
        return True
    argument = pieces[1] if len(pieces) > 1 else ""
    try:
        coverage = ResearchCoverage(memory.path)
        changed = False
        if command == "/watchlist":
            print(json.dumps(coverage.list(query=argument, limit=100), ensure_ascii=False, indent=2))
            print("最多显示100项；可在 /watchlist 后输入股票代码或关键词筛选。")
        elif command == "/watch":
            parts = argument.split(maxsplit=1)
            if not parts:
                raise ValueError("用法：/watch 股票代码 [备注]")
            row = coverage.add(parts[0], note=parts[1] if len(parts) > 1 else "")
            print(f"已关注 {row['symbol']}。用 /thesis 股票代码 研究论点 保存观点。")
            changed = True
        elif command == "/unwatch":
            changed = coverage.remove(argument)
            print("已移出自选股。" if changed else "该股票不在自选股中。")
        else:
            parts = argument.split(maxsplit=1)
            if not parts:
                raise ValueError("用法：/thesis 股票代码 [研究论点或JSON字段]")
            if len(parts) == 1:
                row = coverage.get(parts[0])
                if row is None:
                    raise ValueError("尚未关注该股票；先使用 /watch 添加。")
            else:
                changes = json.loads(parts[1]) if parts[1].lstrip().startswith("{") else {"thesis": parts[1]}
                if not isinstance(changes, dict):
                    raise ValueError("研究字段必须是JSON对象")
                row = coverage.update(parts[0], **changes)
                changed = bool(changes)
            print(json.dumps(row, ensure_ascii=False, indent=2))
        if changed and on_change is not None:
            on_change()
    except (ValueError, TypeError, sqlite3.Error, OSError) as exc:
        print(f"自选股操作未完成：{exc}")
    return True
