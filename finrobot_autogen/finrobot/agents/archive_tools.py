"""Research archive tools, local commands and per-turn persistence."""
import json
import sqlite3
from typing import Annotated

from autogen import register_function

from .coverage import normalize_symbol
from .research_archive import ResearchArchive


def save_research_turn(memory, question, answer, roles, chats, status):
    """An archive failure must not discard a saved turn or its visible answer."""
    try:
        turn_id = memory.save_turn(question, answer, roles, chats, status=status)
    except (sqlite3.Error, OSError, ValueError) as exc:
        return {"memory_error": type(exc).__name__}
    result = {"turn_id": turn_id}
    if status == "completed":
        try:
            result["report_id"] = ResearchArchive(memory.path).archive_turn(turn_id)["id"]
        except (sqlite3.Error, OSError, ValueError) as exc:
            result["archive_error"] = type(exc).__name__
    return result


def register_archive_tools(assistant, memory):
    def find_reports(
        symbol: Annotated[str, "Optional A-share code or US ticker"] = "",
        query: Annotated[str, "Optional literal keywords from earlier research"] = "",
        limit: Annotated[int, "Maximum reports from 1 to 10"] = 5,
    ) -> str:
        """Find saved research versions; requested_symbols and tool-evidence symbols are labelled separately."""
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("limit must be an integer from 1 to 10")
        return json.dumps(ResearchArchive(memory.path).find(symbol=symbol or None, query=query,
                                                           limit=limit, for_model=True), ensure_ascii=False)

    def read_report(report_id: Annotated[int, "Saved report ID returned by find_reports"]) -> str:
        """Read a saved research snapshot and its source evidence; it is not fresh market data."""
        report = ResearchArchive(memory.path).get(report_id, for_model=True)
        return json.dumps(report if report else {"error": "Report not found; use find_reports"}, ensure_ascii=False)

    def diff_reports(
        a_id: Annotated[int, "Earlier saved report ID"],
        b_id: Annotated[int, "Later saved report ID"],
    ) -> str:
        """Compare saved answers and verified metric scopes; do not infer why results changed."""
        return json.dumps(ResearchArchive(memory.path).compare(a_id, b_id, for_model=True), ensure_ascii=False)

    for function in (find_reports, read_report, diff_reports):
        register_function(function, caller=assistant.assistant, executor=assistant.user_proxy,
                          description=function.__doc__)


def handle_archive_command(message, memory):
    pieces = message.strip().split(maxsplit=1)
    command = pieces[0] if pieces else ""
    if command not in {"/reports", "/report", "/compare", "/archive"}:
        return False
    if memory is None:
        print("Memory 已关闭；去掉 --no-memory 后可查询和保存研究归档。")
        return True
    argument = pieces[1] if len(pieces) > 1 else ""
    try:
        archive = ResearchArchive(memory.path)
        if command == "/reports":
            query_parts = argument.split(maxsplit=1)
            if query_parts and query_parts[0] == "--query":
                if len(query_parts) < 2:
                    raise ValueError("用法：/reports --query 关键词")
                result = archive.find(query=query_parts[1], limit=20)
            else:
                try:
                    symbol = normalize_symbol(argument) if argument else None
                except ValueError:
                    symbol = None
                result = archive.find(symbol=symbol, query=argument if not symbol else "", limit=20)
        elif command == "/report":
            result = archive.get(int(argument))
            if result is None:
                raise ValueError("没有找到该研究；先用 /reports 查询ID。")
        elif command == "/compare":
            ids = argument.split()
            if len(ids) != 2:
                raise ValueError("用法：/compare 较早研究ID 较晚研究ID")
            result = archive.compare(int(ids[0]), int(ids[1]))
        else:
            if not argument:
                raise ValueError("用法：/archive 对话轮次ID；可用 /recall 查询旧轮次的id。")
            result = archive.archive_turn(int(argument))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(f"研究归档操作未完成：{exc}")
    return True
