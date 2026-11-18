"""FinRobot V0 快速启动脚本（用 finrobot conda 环境运行）。

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
import os
import re
from pathlib import Path

import autogen

from finrobot.agents.workflow import SingleAssistant
from finrobot.data_source import describe_provider
from finrobot.utils import register_keys_from_json

BASE_DIR = Path(__file__).resolve().parent

TERMINATE_HINT = "\n\n（完成回答后，请在最后一行单独输出 TERMINATE 以结束本次对话。）"

EXIT_COMMANDS = {"exit", "quit", ":q", "/exit"}


def with_terminate_hint(prompt: str) -> str:
    if "TERMINATE" in prompt.upper():
        return prompt
    return prompt + TERMINATE_HINT


def _final_answer(result):
    """最后一个不含工具调用的 assistant 文本消息，去掉结尾的 TERMINATE 行。"""
    for msg in reversed(result.chat_history):
        if msg.get("role") == "assistant" and msg.get("content") and "tool_calls" not in msg:
            content = re.sub(r"(?m)^\s*TERMINATE\s*$", "", msg["content"]).strip()
            if content:
                return content
    return ""


def _called_tools(result):
    """按出现顺序去重后的工具名列表。"""
    names = []
    for msg in result.chat_history:
        for call in msg.get("tool_calls") or []:
            name = call.get("function", {}).get("name")
            if name and (not names or names[-1] != name):
                names.append(name)
    return names


def ask(assistant: SingleAssistant, message: str, clear_history: bool = False):
    is_quant = assistant.assistant.name == "Quant_Analyst"
    # 直接调用 initiate_chat 并保留历史，不用 chat()（它结束后会 reset 清空上下文）。
    # Quant_Analyst 走 silent：屏蔽 AutoGen 原始的工具调用/返回 JSON 刷屏，只保留
    # “EXECUTING FUNCTION ...” 进度行，结束后再单独打印干净的回答。
    result = assistant.user_proxy.initiate_chat(
        assistant.assistant,
        message=with_terminate_hint(message),
        clear_history=clear_history,
        silent=is_quant,
    )
    if is_quant:
        import json
        from datetime import datetime, timezone
        folder = BASE_DIR.parent / "artifacts/quant_dialogue/chats"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".json")
        path.write_text(json.dumps(result.chat_history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        tools = _called_tools(result)
        if tools:
            print("\n已调用工具：" + " → ".join(tools))
        answer = _final_answer(result)
        if answer:
            print("\n" + answer)
        print(f"\n（对话记录已保存：{path}）")
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

        ask(assistant, message, clear_history=first_turn)
        first_turn = False


def main():
    parser = argparse.ArgumentParser(description="Run a FinRobot V0 agent")
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

    assistant = SingleAssistant(
        args.role,
        llm_config=llm_config,
        **({"code_execution_config": False} if args.role == "Quant_Analyst" else {}),
        # AutoGen 约定：收到含 TERMINATE 的回复即结束本轮对话
        is_termination_msg=lambda x: "TERMINATE" in (x.get("content") or ""),
    )

    if args.prompt:
        ask(assistant, args.prompt, clear_history=True)
    else:
        repl(assistant)


if __name__ == "__main__":
    main()
