# 本地环境状态

更新：2026-11-18（Asia/Shanghai）。环境位于本项目 `.venv/`，独立于系统 Python。

## 已完成

- 新建 Python 3.11.16 虚拟环境，安装完整依赖和可编辑 FinRobot 0.1.5。
- 配置本地 DeepSeek Key，使用官方 `https://api.deepseek.com/v1`，默认模型为 `deepseek-flash`。
- 保留现有 Tushare 配置为空；当前自动使用 AkShare。
- 下载 NLTK `punkt`、`averaged_perceptron_tagger` 到项目 `.cache/nltk_data/`。
- 增加根目录启动入口 `run_finrobot.cmd`，自动使用正确解释器、工作目录、UTF-8 和项目 NLTK 数据路径。`DEBUG=false` 用于避免宿主的 `DEBUG=release` 与 OCR 库的布尔配置冲突。

## 已验证

| 检查 | 结果 |
| --- | --- |
| `.venv/Scripts/python.exe -m pip check` | `No broken requirements found.` |
| `run_finrobot.cmd --help` | 正常输出启动参数并退出，退出码 0 |
| 核心依赖及 `finrobot.functional.ragquery` | 13 个模块均导入成功 |
| `A_Share_Analyst` / `Market_Analyst` / `Expert_Investor` | 均初始化成功，分别注册 24 / 7 / 16 个工具；该检查未调用模型 |
| DeepSeek 官方模型列表 | HTTP 200，包含 `deepseek-flash` |
| OpenAI SDK 调用 DeepSeek | 实际返回 `OK`，本次请求共 9 token |
| 根目录启动脚本 → AutoGen → DeepSeek | 实际返回 `OK` 和 `TERMINATE`，正常退出，退出码 0 |
| PyTorch | 已导入 `2.5.1+cpu`，使用 CPU 版本 |

核心模块离线导入本次累计约 21 秒；启动和模型回复时间会随磁盘缓存、网络及问题长度变化。

模块与角色检查明细见 `tmp/environment-smoke.json`，SDK 连通性结果见 `tmp/deepseek-connection.json`。这些结果不包含 Key。

## 兼容性调整

`constraints-windows-py311.txt` 记录主动限制的版本，`requirements-windows-py311.lock.txt` 记录完整安装版本；锁定文件不含本机可编辑安装路径。重建命令见 README。

- 使用 AutoGen 0.2.35，匹配项目的 `autogen.agentchat.contrib` 接口。
- 使用 OpenAI 1.55.3 / httpx 0.27.2，放宽原依赖表中冲突的 `typing_extensions==4.9.0`。
- 补齐源码导入的 `langchain-chroma`，使用 Chroma 0.5.23。
- 使用 PyTorch 2.5.1 / ONNX Runtime 1.19.2，解决新版原生库在导入 pandas 后出现的 Windows DLL 初始化失败。
- 使用 Marker 0.2.15 / Surya OCR 0.4.14，匹配源码中的旧接口。
- DeepSeek 本地配置设置 `extra_body.thinking.type=disabled`，适配 AutoGen 0.2 的消息结构；官方参数说明见 [DeepSeek Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)。

## 使用

在项目根目录 PowerShell 中运行：

```powershell
.\run_finrobot.cmd
# 或单次提问
.\run_finrobot.cmd "分析一下 600519"
```

本地 Key 保存在 `finrobot_autogen/OAI_CONFIG_LIST`，该文件已被 `.gitignore` 排除。原项目中转模型的 Key 仍为占位符。

## 未验证项和现存提示

- V2 新增的真实行情、传统回测、代码生成及评估已验证，并已完成 3 标的 × 3 时段的 45 次冻结代码回放；22 项测试及独立 Python 环境复验通过，详见 [项目状态](PROJECT_STATUS.md)。本文上方保留环境初始验证记录。
- 任意市场/任意策略的通用性、LLM 样本外策略选择、完整基本面分析、PDF 报告及 OCR 模型推理尚未验证。OCR/embedding 权重未预下载；`wkhtmltopdf` 未在 PATH 中找到，仅 Python `pdfkit` 包已安装。
- FLAML 会提示未安装可选 AutoML 功能；本次三个角色初始化和实际对话均正常。
- AutoGen 会提示未内置 DeepSeek 的价格，框架显示的 cost=0 不能作为实际计费依据。
