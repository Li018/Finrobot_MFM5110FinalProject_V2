"""FinRobot 命令行入口：默认按问题自动分派角色并显示最终分析，--verbose 显示调试过程。

默认按问题自动分派角色；使用 --mode single 或仅指定 --role 可运行单角色。

用法:
    # 自动分派，交互模式
    python run_agent.py

    # 自动分派，单次模式
    python run_agent.py "综合分析 600519 的基本面、近期新闻和波动率"

    # 固定单角色
    python run_agent.py "600519 最近为什么涨" --role Market_Analyst

可用角色：A_Share_Analyst（基本面）、Market_Analyst（市场/情绪）、
Expert_Investor（研究报告）、A_Share_Factor_Researcher（因子/动量回测）、
A_Share_Factor_Ideator（因子创意与规则回测）、Quant_Analyst（固定数据 V2 实验）。
"""
import argparse
import contextlib
import json
import logging
import os
import re
import sys
import traceback
import warnings
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

BASE_DIR = Path(__file__).resolve().parent

with warnings.catch_warnings(record="--verbose" not in sys.argv) as startup_warnings:
    import autogen

    from finrobot.agents.research_router import AutoResearch, DEFAULT_ROLE
    from finrobot.agents.workflow import SingleAssistant
    from finrobot.agents.memory import ResearchMemory
    from finrobot.agents.memory_tools import context_text, register_memory_tool, save_turn
    from finrobot.agents.coverage_tools import handle_coverage_command
    from finrobot.data_source import describe_provider
    from finrobot.utils import register_keys_from_json

if startup_warnings:
    startup_log = BASE_DIR.parent / ".cache/agent_startup.log"
    startup_log.parent.mkdir(parents=True, exist_ok=True)
    with startup_log.open("a", encoding="utf-8") as stream:
        for warning in startup_warnings:
            stream.write(f"{warning.category.__name__}: {warning.message}\n")

TERMINATE_HINT = "\n\n（完成回答后，请在最后一行单独输出 TERMINATE 以结束本次对话。）"

EXIT_COMMANDS = {"exit", "quit", ":q", "/exit"}


def with_terminate_hint(prompt: str) -> str:
    if "TERMINATE" in prompt.upper():
        return prompt
    return prompt + TERMINATE_HINT


def _final_answer(result, assistant_name="Quant_Analyst", start_index=0, require_termination=False):
    """AutoGen proxy histories can label the analyst's final message as role=user."""
    messages = result.chat_history[start_index:]
    last_tool = max((i for i, msg in enumerate(messages)
                     if msg.get("tool_calls") or msg.get("function_call") or msg.get("role") in ("tool", "function")), default=-1)
    for msg in reversed(messages[last_tool + 1:]):
        author = msg.get("name")
        is_analyst = author == assistant_name or (msg.get("role") == "assistant" and not author)
        if is_analyst and isinstance(msg.get("content"), str) and not msg.get("tool_calls") and not msg.get("function_call"):
            if require_termination and not re.search(r"(?:^|\n)\s*TERMINATE\s*$", msg["content"]):
                return ""
            content = re.sub(r"(?m)^\s*TERMINATE\s*$", "", msg["content"]).strip()
            content = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", content)
            if content:
                return content
    return ""


def _called_tools(result, start_index=0):
    """按出现顺序去重后的工具名列表。"""
    names = []
    for msg in result.chat_history[start_index:]:
        for call in msg.get("tool_calls") or []:
            name = call.get("function", {}).get("name")
            if name and name not in names:
                names.append(name)
    return names


@contextlib.contextmanager
def _capture_output(path):
    """Capture print/progress/warnings and handlers bound to the original console."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        root_logger = logging.getLogger()
        loggers = [root_logger] + [obj for obj in logging.Logger.manager.loggerDict.values() if isinstance(obj, logging.Logger)]
        saved = [(logger, logger.handlers[:], logger.propagate) for logger in loggers]
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        for logger in loggers:
            logger.handlers = []
            logger.propagate = True
        root_logger.handlers = [handler]
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                yield
        finally:
            for logger, handlers, propagate in saved:
                logger.handlers = handlers
                logger.propagate = propagate
            for obj in list(logging.Logger.manager.loggerDict.values()):
                if isinstance(obj, logging.Logger) and obj not in loggers:
                    obj.handlers = [h for h in obj.handlers if getattr(h, "stream", None) is not stream]


def ask(assistant: SingleAssistant, message: str, clear_history: bool = False):
    clear_history = clear_history or getattr(assistant, "memory_reset_history", False)
    assistant.memory_reset_history = False
    is_quant = assistant.assistant.name == "Quant_Analyst"
    verbose = getattr(assistant, "show_debug", False)
    start_index = 0 if clear_history else len(assistant.user_proxy.chat_messages.get(assistant.assistant, []))
    folder = BASE_DIR.parent / ("artifacts/quant_dialogue/chats" if is_quant else "artifacts/agent_chats")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    memory = getattr(assistant, "research_memory", None)
    if memory is not None:
        # Replace the temporary system context on every turn. Persist only the
        # original question, so forgetting a note does not leave old injected
        # notes inside the proxy's retained user messages.
        if not hasattr(assistant, "memory_base_prompt"):
            assistant.memory_base_prompt = assistant.assistant.system_message
        try:
            context = context_text(memory, message, include_recent=True)
        except (sqlite3.Error, OSError, ValueError) as exc:
            context = ""
            print(f"本轮记忆暂时不可读（{type(exc).__name__}），继续处理当前问题。")
        assistant.assistant.update_system_message(assistant.memory_base_prompt + "\n\n" + context)
    print("\n正在分析，请稍候…", flush=True)
    try:
        with contextlib.nullcontext() if verbose else _capture_output(path.with_suffix(".log")):
            try:
                result = assistant.user_proxy.initiate_chat(
                    assistant.assistant, message=with_terminate_hint(message),
                    clear_history=clear_history, silent=not verbose)
            except Exception:
                if not verbose:
                    traceback.print_exc()
                raise
    except Exception as exc:
        if memory is not None:
            error = save_turn(memory, message, "", [assistant.assistant.name], [], "failed")
            if error:
                print(f"本轮记忆未保存（{error}）。")
        details = "详见上方调试输出。" if verbose else f"详细原因：{path.with_suffix('.log')}"
        print(f"\n本轮分析未完成（{type(exc).__name__}）。{details}")
        raise
    import json
    path.with_suffix(".json").write_text(json.dumps(result.chat_history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    answer = _final_answer(result, assistant.assistant.name, start_index)
    if memory is not None:
        error = save_turn(memory, message, answer, [assistant.assistant.name],
                          [{"role": assistant.assistant.name, "history": result.chat_history[start_index:]}],
                          "completed" if _final_answer(result, assistant.assistant.name, start_index,
                                                       require_termination=True) else "partial")
        if error:
            print(f"本轮记忆未保存（{error}），分析结果仍可查看。")
    if answer:
        path.with_suffix(".md").write_text(answer + "\n", encoding="utf-8")
        print("\n" + answer)
        print(f"\n分析已保存：{path.with_suffix('.md')}")
    else:
        print(f"\n本轮尚未生成最终分析，请重试。详细过程：{path.with_suffix('.log')}")
    if verbose:
        print("已调用工具：" + " → ".join(_called_tools(result, start_index)))
    return result


def interactive_questions():
    while True:
        try:
            message = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not message:
            continue
        if message.lower() in EXIT_COMMANDS:
            break
        yield message


def repl(assistant: SingleAssistant):
    print(f"已进入交互模式（角色: {assistant.assistant.name}）。输入 exit / quit / :q 退出。")
    if assistant.assistant.name == "Quant_Analyst":
        print("示例：回测000651在2023-2024年的10/30均线，本金10万，开仓预算60%，比较买入持有并检查风险。")
        print("后续可输入：继续生成并运行同规则Python代码，再检查生成策略的风险。")
    first_turn = True
    for message in interactive_questions():
        if handle_local_command(message, getattr(assistant, "research_memory", None), assistant=assistant):
            continue
        try:
            ask(assistant, message, clear_history=first_turn)
        except Exception:
            if getattr(assistant, "show_debug", False):
                raise
            first_turn = False
            continue
        first_turn = False


def ask_auto(research, question, verbose=False):
    import json

    folder = BASE_DIR.parent / "artifacts/agent_chats"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + "_" + uuid4().hex[:8])
    roles = research.plan(question)
    print("\n正在分析，请稍候…", flush=True)
    try:
        with contextlib.nullcontext() if verbose else _capture_output(path.with_suffix(".log")):
            print("分派角色：" + "、".join(roles), flush=True)
            answer = research.answer(question, roles)
    except Exception as exc:
        record = {"ok": False, "question": question, "roles": list(roles), "error_type": type(exc).__name__}
        path.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n本轮分析未完成（{type(exc).__name__}）。记录：{path.with_suffix('.json')}")
        raise
    record = research.last_run
    path.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.with_suffix(".md").write_text(answer + "\n", encoding="utf-8")
    print("\n" + answer)
    if not record["ok"]:
        print("\n本轮包含未完成的分项，请查看记录中的错误类型。")
    if record.get("memory_error"):
        print(f"\n本轮记忆未保存（{record['memory_error']}），分析结果仍可查看。")
    if record.get("memory_read_error"):
        print(f"\n本轮未能读取记忆（{record['memory_read_error']}），已依据当前问题和本次会话作答。")
    print(f"\n本轮记录已保存：{path.with_suffix('.md')}")
    return record


def run_auto(llm_config, prompt, verbose=False, memory=None):
    research = AutoResearch(llm_config, memory=memory) if memory is not None else AutoResearch(llm_config)
    if prompt is None:
        print("已进入交互模式（自动分派）。输入 exit / quit / :q 退出。")
    for question in interactive_questions() if prompt is None else (prompt,):
        if handle_local_command(question, memory):
            continue
        try:
            record = ask_auto(research, question, verbose)
        except Exception:
            if verbose:
                raise
            if prompt is not None:
                raise SystemExit(1)
            continue
        if prompt is not None and not record["ok"]:
            raise SystemExit(1)


def clear_live_history(assistant):
    if assistant is not None:
        assistant.assistant.clear_history()
        assistant.user_proxy.clear_history()
        assistant.memory_reset_history = True


def handle_local_command(message, memory, *, assistant=None):
    return (handle_memory_command(message, memory, assistant=assistant)
            or handle_coverage_command(message, memory, on_change=lambda: clear_live_history(assistant)))


def handle_memory_command(message, memory, *, assistant=None):
    command, _, argument = message.strip().partition(" ")
    if command not in {"/memory", "/remember", "/recall", "/forget", "/sessions"}:
        return False
    if memory is None:
        print("Memory 已关闭；去掉 --no-memory 后可使用记忆命令。")
        return True
    try:
        if command == "/remember":
            note = memory.remember(argument.strip())
            print(f"已保存笔记 #{note['id']}：{note['text']}")
        elif command == "/forget":
            deleted = memory.forget(int(argument.strip()))
            if deleted and assistant is not None:
                # A previous recall tool response can still contain this note.
                # Rebuild the next turn from the store, not stale tool messages.
                clear_live_history(assistant)
            print("笔记已删除。" if deleted else "没有找到该笔记。")
        elif command == "/recall":
            print(json.dumps(memory.recall(argument.strip()), ensure_ascii=False, indent=2))
        elif command == "/sessions":
            print(json.dumps(memory.sessions(), ensure_ascii=False, indent=2))
        else:
            print(f"当前会话：{memory.session_id}")
            print(json.dumps(memory.context("", include_recent=False), ensure_ascii=False, indent=2))
            print("/remember 内容 保存笔记；/recall 股票或关键词 查询历史；/forget ID 删除笔记；/sessions 查看会话。")
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(f"记忆操作未完成：{exc}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Run a FinRobot agent")
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="单次提问；省略则进入交互模式",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "single"],
        default=None,
        help="auto：按问题分派（默认）；single：固定单角色。仅指定 --role 时沿用 single。",
    )
    parser.add_argument(
        "--role",
        default=None,
        help="固定单角色：A_Share_Analyst / Market_Analyst / Expert_Investor / "
             "A_Share_Factor_Researcher / A_Share_Factor_Ideator / Quant_Analyst",
    )
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--verbose", action="store_true", help="显示工具调用和调试信息；默认仅显示最终分析")
    parser.add_argument("--session", help="继续指定会话；省略则创建新会话")
    parser.add_argument("--no-memory", action="store_true", help="关闭持久记忆，不读取或写入记忆数据库")
    parser.add_argument("--memory-db", type=Path, default=BASE_DIR.parent / "data/memory/research.sqlite3",
                        help="本地记忆数据库位置；相对路径按仓库根目录解析")
    parser.add_argument(
        "--provider",
        choices=["auto", "akshare", "tushare"],
        default=None,
        help="A 股数据源：auto（默认，优先 Tushare，失败回退 AkShare）/ akshare / tushare",
    )
    args = parser.parse_args()
    if args.mode == "auto" and args.role:
        parser.error("--mode auto 不能与 --role 一起使用；固定角色请使用 --mode single。")
    mode = args.mode or ("single" if args.role else "auto")
    if args.no_memory and args.session:
        parser.error("--no-memory 不能与 --session 一起使用。")
    memory = None
    if not args.no_memory:
        try:
            memory_path = args.memory_db.expanduser()
            if not memory_path.is_absolute():
                memory_path = BASE_DIR.parent / memory_path
            memory = ResearchMemory(memory_path, session_id=args.session, mode=mode,
                                    role=(args.role or DEFAULT_ROLE) if mode == "single" else None)
        except (ValueError, sqlite3.Error, OSError) as exc:
            parser.error(f"无法打开记忆：{exc}。可用 --no-memory 临时关闭。")
        resume_args = f"--session {memory.session_id}"
        if mode == "single":
            resume_args += f" --role {args.role or DEFAULT_ROLE}"
        print(f"会话：{memory.session_id}（{resume_args} 可继续；使用相同 --memory-db）")
    # Local memory management must also work before API keys are configured.
    if args.prompt is not None and handle_local_command(args.prompt, memory):
        return

    register_keys_from_json(str(BASE_DIR / "config_api_keys"))
    if args.provider:
        os.environ["CN_DATA_PROVIDER"] = args.provider
    print(describe_provider())

    llm_config = {
        "config_list": autogen.config_list_from_json(
            str(BASE_DIR / "OAI_CONFIG_LIST"),
            filter_dict={"model": [args.model]},
        ),
        "timeout": 120,
        "temperature": 0,
    }
    if args.role == "Quant_Analyst":
        llm_config.update(cache_seed=None, max_tokens=3500)

    if mode == "auto":
        run_auto(llm_config, args.prompt, verbose=args.verbose, memory=memory)
        return

    with contextlib.nullcontext() if args.verbose else _capture_output(BASE_DIR.parent / ".cache/agent_startup.log"):
        assistant = SingleAssistant(
            args.role or DEFAULT_ROLE, llm_config=llm_config,
            **({"code_execution_config": False} if args.role == "Quant_Analyst" else {}),
            is_termination_msg=lambda x: "TERMINATE" in (x.get("content") or ""))
    assistant.show_debug = args.verbose
    if memory is not None:
        assistant.research_memory = memory
        register_memory_tool(assistant, memory)

    if args.prompt:
        try:
            ask(assistant, args.prompt, clear_history=True)
        except Exception:
            if args.verbose:
                raise
            raise SystemExit(1)
    else:
        repl(assistant)


if __name__ == "__main__":
    main()
