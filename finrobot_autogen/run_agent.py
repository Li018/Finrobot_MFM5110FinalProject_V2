"""FinRobot 命令行入口；默认显示最终分析，--verbose 显示调试过程。

默认使用 A 股角色 ``A_Share_Analyst``（数据来自 CNStockUtils，自动路由 AkShare / Tushare Pro）。

用法:
    # 交互模式（多轮对话，保留上下文）
    python run_agent.py --role A_Share_Analyst

    # 单次模式
    python run_agent.py "帮我分析一下 600519 最近一年的基本面和估值" --role A_Share_Analyst

    # 其他角色：Market_Analyst（市场/情绪）、Expert_Investor（研究报告）
    python run_agent.py "600519 最近为什么涨" --role Market_Analyst
"""
import argparse
import contextlib
import logging
import os
import re
import sys
import traceback
import warnings
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

with warnings.catch_warnings(record="--verbose" not in sys.argv) as startup_warnings:
    import autogen
    from finrobot.agents.workflow import SingleAssistant
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


def _final_answer(result, assistant_name="Quant_Analyst", start_index=0):
    """AutoGen proxy histories can label the analyst's final message as role=user."""
    messages = result.chat_history[start_index:]
    last_tool = max((i for i, msg in enumerate(messages)
                     if msg.get("tool_calls") or msg.get("function_call") or msg.get("role") in ("tool", "function")), default=-1)
    for msg in reversed(messages[last_tool + 1:]):
        author = msg.get("name")
        is_analyst = author == assistant_name or (msg.get("role") == "assistant" and not author)
        if is_analyst and isinstance(msg.get("content"), str) and not msg.get("tool_calls") and not msg.get("function_call"):
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
    is_quant = assistant.assistant.name == "Quant_Analyst"
    verbose = getattr(assistant, "show_debug", False)
    start_index = 0 if clear_history else len(assistant.user_proxy.chat_messages.get(assistant.assistant, []))
    folder = BASE_DIR.parent / ("artifacts/quant_dialogue/chats" if is_quant else "artifacts/agent_chats")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
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
        details = "详见上方调试输出。" if verbose else f"详细原因：{path.with_suffix('.log')}"
        print(f"\n本轮分析未完成（{type(exc).__name__}）。{details}")
        raise
    import json
    path.with_suffix(".json").write_text(json.dumps(result.chat_history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    answer = _final_answer(result, assistant.assistant.name, start_index)
    if answer:
        path.with_suffix(".md").write_text(answer + "\n", encoding="utf-8")
        print("\n" + answer)
        print(f"\n分析已保存：{path.with_suffix('.md')}")
    else:
        print(f"\n本轮尚未生成最终分析，请重试。详细过程：{path.with_suffix('.log')}")
    if verbose:
        print("已调用工具：" + " → ".join(_called_tools(result, start_index)))
    return result


def repl(assistant: SingleAssistant):
    print(f"已进入交互模式（角色: {assistant.assistant.name}）。输入 exit / quit / :q 退出。")
    if assistant.assistant.name == "Quant_Analyst":
        print("示例：回测000651在2023-2024年的10/30均线，本金10万，开仓预算60%，比较买入持有并检查风险。")
        print("后续可输入：继续生成并运行同规则Python代码，再检查生成策略的风险。")
    first_turn = True
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

        try:
            ask(assistant, message, clear_history=first_turn)
        except Exception:
            if getattr(assistant, "show_debug", False):
                raise
            first_turn = False
            continue
        first_turn = False


def main():
    parser = argparse.ArgumentParser(description="Run a FinRobot agent")
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="单次提问；省略则进入交互模式",
    )
    parser.add_argument(
        "--role",
        default="A_Share_Analyst",
        help="角色名：A_Share_Analyst（默认）/ Market_Analyst / Expert_Investor / Quant_Analyst",
    )
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--verbose", action="store_true", help="显示工具调用和调试信息；默认仅显示最终分析")
    parser.add_argument(
        "--provider",
        choices=["auto", "akshare", "tushare"],
        default=None,
        help="A 股数据源：auto（默认，优先 Tushare，失败回退 AkShare）/ akshare / tushare",
    )
    args = parser.parse_args()

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

    with contextlib.nullcontext() if args.verbose else _capture_output(BASE_DIR.parent / ".cache/agent_startup.log"):
        assistant = SingleAssistant(
            args.role, llm_config=llm_config,
            **({"code_execution_config": False} if args.role == "Quant_Analyst" else {}),
            is_termination_msg=lambda x: "TERMINATE" in (x.get("content") or ""))
    assistant.show_debug = args.verbose

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
