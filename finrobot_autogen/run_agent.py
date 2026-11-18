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


def ask(assistant: SingleAssistant, message: str, clear_history: bool = False):
    # 直接调用 initiate_chat 并保留历史，不用 chat()（它结束后会 reset 清空上下文）
    assistant.user_proxy.initiate_chat(
        assistant.assistant,
        message=with_terminate_hint(message),
        clear_history=clear_history,
    )


def repl(assistant: SingleAssistant):
    print(f"已进入交互模式（角色: {assistant.assistant.name}）。输入 exit / quit / :q 退出。")
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
        help="角色名：A_Share_Analyst（默认）/ Market_Analyst / Expert_Investor",
    )
    parser.add_argument("--model", default="gpt-4o")
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

    assistant = SingleAssistant(
        args.role,
        llm_config=llm_config,
        # AutoGen 约定：收到含 TERMINATE 的回复即结束本轮对话
        is_termination_msg=lambda x: "TERMINATE" in (x.get("content") or ""),
    )

    if args.prompt:
        ask(assistant, args.prompt, clear_history=True)
    else:
        repl(assistant)


if __name__ == "__main__":
    main()
