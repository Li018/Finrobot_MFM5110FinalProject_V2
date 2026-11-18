# 配置文件说明（运行前必读）

本项目的两个配置文件就在本目录（`finrobot_autogen/`）下：

| 文件 | 作用 | 是否必须填写 |
| --- | --- | --- |
| `OAI_CONFIG_LIST` | 大模型（LLM）的 API Key | **必须**（至少填一个模型） |
| `config_api_keys` | A 股数据源（Tushare）等 API Key | 可选（不填自动用免费的 AkShare） |

> 注意：这两个文件是 JSON 格式，**JSON 不支持 `#` 或 `//` 注释**。因此每项需要配置的内容都用
> `REPLACE_ME_...` 占位符写在了对应字段里，把占位符替换成你自己的 key 即可。本文档就是详细说明。

---

## 1. `OAI_CONFIG_LIST` —— 大模型 API

文件内容是一个数组，每个对象是一路可用的模型服务：

```json
{
    "model": "模型名",
    "api_key": "你的 API Key",
    "base_url": "服务地址（官方 OpenAI 可省略或写 https://api.openai.com/v1）"
}
```

### 1.1 DeepSeek（推荐，便宜）

- 网站：https://platform.deepseek.com
- 步骤：注册/登录 → 左侧「API Keys」→「创建 API Key」→ 复制 `sk-` 开头的字符串
- 把 `deepseek-flash` 那一项的 `api_key` 替换成你的 key 即可，`base_url` 保持不变
- 用 DeepSeek 运行时需要指定模型：
  ```bash
  python run_agent.py "分析一下 600519" --role A_Share_Analyst --model deepseek-flash
  ```

### 1.2 gpt-4o / gpt-4o-mini（项目共享中转）

- 文件里这两项的 `base_url` 是项目组共用的 OpenAI 兼容中转地址
  （`http://117.72.29.113:4000/v1`），对应的 key 请**找项目负责人 / 课程群里索取**，
  把 `REPLACE_ME_PROJECT_PROXY_KEY` 替换掉。
- 如果你自己用官方 OpenAI，把这两项改成：
  ```json
  {
      "model": "gpt-4o",
      "api_key": "sk-你的OpenAIKey",
      "base_url": "https://api.openai.com/v1"
  }
  ```
  官方 key 获取：https://platform.openai.com/api-keys
- `run_agent.py` 默认使用 `--model gpt-4o`，所以想用默认参数直接跑，需要先配好这一项。

### 1.3 最小配置

只填 `deepseek-flash` 一项即可运行，只是启动时要加 `--model deepseek-flash`：

```bash
cd finrobot_autogen
python run_agent.py --role A_Share_Analyst --model deepseek-flash
```

---

## 2. `config_api_keys` —— 数据源 API（可选）

```json
{
    "TUSHARE_TOKEN": "",
    "CN_DATA_PROVIDER": "auto"
}
```

- **`TUSHARE_TOKEN`（可选）**：留空即可，程序会自动使用免费的 **AkShare** 拉取 A 股数据。
  想用 Tushare Pro（数据更全、有积分限制）时：
  1. 注册：https://tushare.pro/register
  2. 登录后在个人主页复制 token：https://tushare.pro/user/token
  3. 粘贴到 `TUSHARE_TOKEN` 的值里（保留引号）
- **`CN_DATA_PROVIDER`**：
  - `auto`（默认）：优先 Tushare，失败或积分不足自动回退 AkShare
  - `akshare`：只用免费的 AkShare
  - `tushare`：只用 Tushare，失败直接报错（排查问题时用）
- 文件里以 `_README_` 开头的键只是说明文字，程序会把它们当作普通环境变量，不影响运行。

---

## 3. 安装与运行

```bash
# 1) 创建环境（Python 3.10）
conda create -n finrobot python=3.10 -y
conda activate finrobot

# 2) 安装依赖（在仓库根目录）
pip install -r finrobot_autogen/requirements.txt
pip install -e .

# 3) 填好上面两个配置文件后运行
cd finrobot_autogen
python run_agent.py --role A_Share_Analyst --model deepseek-flash
# 或交互模式（不带 prompt 参数）
python run_agent.py --role A_Share_Analyst --model deepseek-flash
```

可用角色：`A_Share_Analyst`（默认，A 股）、`Market_Analyst`（市场/情绪）、`Expert_Investor`（研究报告）。

---

## 4. 安全提示

- 仓库里的这两个文件只是**模板占位符**，请不要把填写了真实 key 的版本提交上去。
- 填好 key 后，可以在本地执行下面命令，让 git 忽略这两个文件的改动，避免误提交：
  ```bash
  git update-index --skip-worktree finrobot_autogen/OAI_CONFIG_LIST finrobot_autogen/config_api_keys
  ```
- 如果以后需要重新提交模板（或解决拉取冲突），先取消标记：
  ```bash
  git update-index --no-skip-worktree finrobot_autogen/OAI_CONFIG_LIST finrobot_autogen/config_api_keys
  ```

---

## 5. 换成其他模型

在 `OAI_CONFIG_LIST` 数组里加一项，运行时用 `--model` 选择：

```json
{
    "model": "qwen-plus",
    "api_key": "你的key",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
}
```

```bash
python run_agent.py "分析一下 600519" --role A_Share_Analyst --model qwen-plus
```

常见服务地址：

| 服务 | base_url | key 申请 |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` | https://platform.deepseek.com/api_keys |
| OpenAI | `https://api.openai.com/v1` | https://platform.openai.com/api-keys |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | https://bailian.console.aliyun.com/ |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | https://open.bigmodel.cn/ |
| 本地 Ollama | `http://localhost:11434/v1`（key 随便填） | 本地运行 `ollama serve` |

注意：

- `--model` 必须和配置里的 `"model"` 完全一致
- 模型必须支持 function calling（工具调用），本地小模型通常不支持
- `run_agent.py` 默认 `--model gpt-4o`，用别的模型一定要加 `--model`
