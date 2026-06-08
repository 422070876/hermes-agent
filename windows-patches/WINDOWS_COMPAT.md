# Hermes Agent Windows 兼容指南

> 让 Hermes Agent 在 Windows 上（Git Bash / WSL）正常工作的补丁包。

| 项目 | 值 |
|------|-----|
| **基于上游版本** | [v0.16.0](https://github.com/NousResearch/hermes-agent) — commit `fa42ac094`（2026-06-07） |
| **补丁更新日期** | 2026-06-08 |
| **兼容范围** | Windows 10 / 11（Git Bash / MSYS2 / WSL） |
| **GitHub 仓库** | https://github.com/422070876/hermes-agent/tree/windows-patches |

---

## 目录

1. [快速安装](#1-快速安装)
2. [问题概述](#2-问题概述)
3. [补丁内容详解](#3-补丁内容详解)
4. [验证安装](#4-验证安装)
5. [补丁冲突处理](#5-补丁冲突处理)
6. [回滚方法](#6-回滚方法)
7. [文件清单](#7-文件清单)

---

## 1. 快速安装

### 一行完成

```bash
cd hermes-agent
git apply windows-patches/hermes-windows.patch
cp windows-patches/browser_cdp_backend.py tools/
pip install websockets
```

只需这 3 条命令即可完成所有 Windows 兼容改造。

### 在新克隆的上游仓库上安装

```bash
git clone https://github.com/NousResearch/hermes-agent.git
cd hermes-agent
git checkout fa42ac094  # Hermes Agent v0.16.0

# 下载补丁包（如果已有 windows-patches 目录则跳过）
mkdir -p windows-patches
# 方法 1: 从 GitHub 下载
curl -Lo windows-patches/hermes-windows.patch \
  https://raw.githubusercontent.com/422070876/hermes-agent/main/windows-patches/hermes-windows.patch
curl -Lo windows-patches/browser_cdp_backend.py \
  https://raw.githubusercontent.com/422070876/hermes-agent/main/windows-patches/browser_cdp_backend.py

# 方法 2: 从本地备份复制
# cp /path/to/backup/windows-patches/hermes-windows.patch windows-patches/
# cp /path/to/backup/windows-patches/browser_cdp_backend.py windows-patches/

# 安装补丁
git apply windows-patches/hermes-windows.patch
cp windows-patches/browser_cdp_backend.py tools/
pip install websockets
```

---

## 2. 问题概述

Hermes Agent 默认针对 Linux/macOS 设计，在 Windows 上存在以下兼容性问题：

| # | 问题 | 表现 | 修复方式 |
|---|------|------|----------|
| 1 | **浏览器不可用** | `agent-browser` 的 Rust daemon 在 Windows 上 CDP 通信阻塞 | Python CDP 后端直接操控 Chrome |
| 2 | **找不到 bash** | 只搜索 PATH 和 3 个 Git 路径，不搜 WSL | 扩展搜索路径 + WSL 回退 |
| 3 | **PATH 分隔符错误** | 硬编码 Unix `:`，Windows 需要用 `;` | 平台感知的 `_PATH_SEP` |
| 4 | **HOME 缺失** | 没有设置 HOME 变量，bash 行为异常 | 从 USERPROFILE 自动推导 |
| 5 | **Shell rc 文件不对** | Linux `~/.profile` 在 Git Bash 下不存在 | Windows 用 `~/.bashrc` |
| 6 | **临时目录不兼容** | `tempfile.gettempdir()` 返回带空格的路径 | 用 `HERMES_HOME/cache/terminal` |
| 7 | **CWD 路径双格式** | Git Bash `/c/Users/...` 和 WSL `/mnt/c/...` 混用 | 统一转回 `C:\Users\...` |
| 8 | **进程杀死不彻底** | `os.killpg` 在 Windows 上不存在 | `taskkill /T /F` + PowerShell 清理 Chrome |
| 9 | **日志轮转权限错误** | `PermissionError` 日志轮转失败 | 用 `shutil.copy2` + 清空替代 rename |
| 10 | **启动工作目录错误** | 终端工具 CWD 默认不在 Hermes 家目录 | 启动时 chdir 到 `get_hermes_home()` |
| 11 | **API 费用显示人民币** | 官方 DeepSeek 定价为 USD，国内用户看 ¥ 方便 | 所有 DeepSeek 定价改为 CNY，状态栏显示 ¥ |
| 12 | **限流回退过慢** | 被限流时也用默认 60s 最大等待 | 限流时 base_delay=3.0, max_delay=20 快速恢复 |
| 13 | **自定义定价配置入口** | 使用本地模型或国内 API 需手动改代码覆盖定价 | `config.yaml` 中加 `custom_pricing` 字段，运行时动态加载 |
| 14 | **execute_code 行号污染** | `read_file()` 返回内容带行号前缀 | 新增 `read_file_raw()` 工具，返回纯文本内容 |
| 15 | **429 重试间隔太短** | SDK 默认重试 2 次零延迟 | 禁用 SDK 内部重试 + 退避后 `continue` |
| 16 | **状态栏缓存率/费用** | 会话运行中看不到缓存命中率和累计费用 | 三种状态栏模式均显示 `缓存 XX%` 和 `¥/¥X.XXXX` |
| 17 | **patch 工具缩进错乱** | `read_file` 截断标记被模型误作文件内容传入 patch，模糊匹配后 `_reindent_replacement` 把缩进算歪 | 截断标记 ` … [TRUNCATED]` + 守卫拒绝含 `[TRUNCATED]` 的 old_string + 内容相似度守卫 + 众数缩进对齐 + 缩进一致性保护 |
| 18 | **技能搜索能力缺失** | `skills_list` 只能列出所有技能名，大模型需要手动扫描数百个技能找到合适的 | 新增 `find_skills(query)` 工具，关键词搜索 + 评分排序 + 子串命中优先 |
| 19 | **推理内容回传浪费 tokens** | 每轮回传 `reasoning_content` 消耗 ~500 计费 tokens 并破坏 DeepSeek 字节级缓存前缀 | 只在需要 thinking pad 的 Provider 回传，减少 token 和缓存命中影响 |
| 20 | **项目上下文文件过度加载** | AGENTS.md/CLAUDE.md/.cursorrules 自动加载浪费 ~7000 tokens/轮 | 改为提示模型按需用 `search_files()`/`read_file()` 加载 |
| 21 | **insufficient_quota 误判 billing** | `insufficient_quota` 被归类为 billing 导致立即耗竭凭证池，但实际上 Alibaba/DashScope 的每日额度满了是临时性速率限制 | 移出 billing 组，新建成单独的 rate_limit 分支，凭据池的连续 429 检测处理真正的计费耗尽 |
| 22 | **CRLF 换行符干扰文本操作** | `read_file_raw`/`read_file` 返回带 CRLF(\r\n) 的内容，Python 的 .split("\n") 留下 \r 尾巴，patch 模糊匹配的缩进计算被 \r 干扰 | 读入时自动 \r\n -> \n 归一化，写回时自动转回 \r\n |

---

## 3. 补丁内容详解

### 3.1 CDP 浏览器后端

**文件**：`tools/browser_tool.py`

**问题**：`agent-browser` CLI 是一个 Rust 程序，其内部的 daemon 进程通过 CDP（WebSocket）操控 Chrome。在 Windows 上，Rust 的 WebSocket 库与 Chrome CDP 通信会阻塞超时，导致所有浏览器操作不可用。

**修复**：3 个修改点

1. **`_create_local_session()`** — Windows 上调用 `browser_cdp_backend.start_chrome_and_get_cdp_url()` 启动 Chrome 并获取 CDP URL
2. **`_run_browser_command()`** — Windows 上通过 Python CDP 客户端路由命令，不走 `agent-browser`
3. **新增 `_run_cdp_command()`** — CDP 命令分发函数，支持 open/snapshot/click/type/scroll/back/press/console/screenshot/get_images/eval/close

**2026-06-08 更新**：`browser_get_images()` 的 JS 代码从箭头函数 (`=>`) 改为 ES5 兼容 IIFE。原因：agent-browser Rust 二进制的 `eval` 不支持箭头函数（语法解析错误），且 CDP 后端 `console()` 返回格式不同（`data.value` 而非 `data.result`）。改动：
- JS 代码改为 ES5 单行 IIFE：`(function(){var r=[];...return JSON.stringify(r)})()`
- `raw_result` 取值改为 `data.get("result") or data.get("value") or "[]"`，兼容两种后端返回格式

### 3.2 Git Bash 检测增强

**文件**：`tools/environments/local.py` / `_find_bash()`

搜索顺序：
1. `HERMES_GIT_BASH_PATH` 环境变量
2. Hermes 内置 PortableGit
3. Git for Windows 标准安装路径
4. `shutil.which("bash")`
5. WSL bash (`System32\bash.exe`)
6. `C:\app\Git\bin\bash.exe`
7. 报错提示安装

### 3.3 PATH / HOME / Shell rc / 临时目录

**文件**：`tools/environments/local.py`

| 修改点 | 函数 | 说明 |
|--------|------|------|
| PATH 分隔符 | 常量 | Windows 用 `;`，Unix 用 `:` |
| HOME 推导 | `_make_run_env()` | `USERPROFILE` → `/c/Users/...` |
| Shell rc 文件 | `_resolve_shell_init_files()` | Windows 用 `~/.bashrc` 优先 |
| 临时目录 | `get_temp_dir()` | Windows 用 `HERMES_HOME/cache/terminal` |

### 3.4 路径转换

**文件**：`tools/environments/local.py`

处理多种路径格式：
- Git Bash: `/k/hermes-agent` → `K:\hermes-agent`
- WSL: `/mnt/k/hermes-agent` → `K:\hermes-agent`
- Python: `K:\hermes-agent` → 直接使用

### 3.5 进程清理

**文件**：`tools/environments/local.py` / `_kill_process()`

Windows 上：`taskkill /T /F /PID` → PowerShell 清理 Chrome 孤儿进程

### 3.6 日志轮转

**文件**：`hermes_logging.py` / `_ManagedRotatingFileHandler.rotate()`

Windows 上 `PermissionError` 时改用 `shutil.copy2` + 清空源文件。

### 3.7 启动工作目录

**文件**：`hermes_cli/main.py` / `main()`

启动时自动 chdir 到 `get_hermes_home()`，确保终端工具的 CWD 正确。

### 3.8 DeepSeek CNY 定价与 Alibaba 路由

**文件**：`agent/usage_pricing.py` / `tests/agent/test_usage_pricing.py`

**问题**：官方 DeepSeek 定价表 (`_OFFICIAL_DOCS_PRICING`) 使用 USD，国内用户习惯 ¥ 计价。通过阿里云 DashScope 调用 DeepSeek 时，缓存价格不同（¥0.2 而非 ¥0.02），但被路由到"custom"导致无法查到定价。

**修复**：
- `PricingEntry` 和 `CostResult` 新增 `currency` 字段（默认 `"USD"`）
- 所有 DeepSeek 模型定价改为 **CNY**：deepseek-chat/v4-flash ¥1 input / ¥2 output，v4-pro ¥3 / ¥6，cache 折扣同步更新
- `resolve_billing_route()` 增加 `alibaba` provider 分支：`base_url` 含 dashscope/aliyun 时路由到 `provider="alibaba"`，走独立的 CNY 定价表
- 新增 `prompt_cache_hit_tokens` 字段解析（DeepSeek 特有）

### 3.9 限流回退策略优化

**文件**：`agent/conversation_loop.py`

**问题**：被限流时（`is_rate_limited=True`）仍使用与普通错误相同的回退策略（base_delay=2, max_delay=60），等待时间过长。

**修复**：被限流时用更激进的回退：`base_delay=3.0, max_delay=20.0`（最快 3s，最多等 20s），非限流错误保留原策略。避免不必要的长时间等待。

### 3.10 状态栏缓存率与费用显示

**文件**：`cli.py` / `hermes_cli/skin_engine.py`

**问题**：会话运行中无法直观看到缓存命中率和累计费用。

**修复**：
- `cli.py` 快照新增 `session_cache_ratio`（缓存占比 %）、`session_cost_usd`（计费金额）、`session_cache_billable`（缓存有无费率）、`session_currency`（货币类型）
- 缓存命中率公式：`cache_read_tokens / (cache_read_tokens + input_tokens)`，即 `cacheHit / (cacheHit + cacheMiss)`
- 状态栏三种渲染模式均显示 `缓存 XX%` 和 `¥/¥X.XXXX`
- `hermes_cli/skin_engine.py` 新增 `status-bar-cache`（绿色）和 `status-bar-cost`（普通文本）样式类

### 3.11 用户自定义定价系统

**文件**：`agent/usage_pricing.py` / `hermes_cli/config.py`

**问题**：使用本地模型或国内 API 时，模型不在内置定价表中，无法显示费用和缓存率；或内置定价与实际情况不符。

**修复**：
- 新增 `_load_custom_pricing()` 函数，从 `config.yaml` 动态加载用户自定义定价
- 支持两种配置来源（后者覆盖前者）：
  1. `model.custom_pricing` — 按裸模型名（如 `deepseek-v4-flash`）
  2. `custom_providers[].models[].custom_pricing` — 按 `provider/model_name` 精确匹配
- 用户配置定价优先级 > OpenRouter > base_url 回退 > 内置定价表
- 输入/输出价格为 0 时视为"免费模型"，状态栏不显示费用
- `CostResult` 新增 `cache_billable` 字段标记模型缓存有无费率
- `hermes_cli/config.py` 的 `DEFAULT_CONFIG` 新增 `"custom_pricing": {}` 占位

**配置示例**（`config.yaml`）：

```yaml
model:
  custom_pricing:
    "qwen3-235b-awq":
      input_cost_per_million: 0
      output_cost_per_million: 0
    "deepseek-v4-flash":
      input_cost_per_million: 1.0
      output_cost_per_million: 2.0
      cache_read_cost_per_million: 0.1
      currency: CNY
    "deepseek-v4-pro":
      input_cost_per_million: 3.0
      output_cost_per_million: 6.0
      cache_read_cost_per_million: 0.3
      currency: CNY
    "anthropic/claude-sonnet-4":
      input_cost_per_million: 3.0
      output_cost_per_million: 15.0
      cache_read_cost_per_million: 0.3

custom_providers:
  - name: alibaba
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    models:
      "deepseek-v4-flash":
        custom_pricing:
          input_cost_per_million: 0.5
          output_cost_per_million: 2.0
          cache_read_cost_per_million: 0.2
          currency: CNY
      "qwen-max":
        custom_pricing:
          input_cost_per_million: 2.0
          output_cost_per_million: 6.0
          currency: CNY
```

**匹配优先级**（高 → 低）：
1. `custom_providers[名称]/model` — 精确匹配 provider + model
2. `model.custom_pricing` 中的裸模型名
3. 传入的完整 `model_name`（如 `custom:alibaba/deepseek-v4-flash`）
4. 内置定价表 / OpenRouter / base_url 回退

### 3.12 execute_code 行号污染修复 — `read_file_raw` 工具

**文件**：`tools/file_tools.py` / `tools/code_execution_tool.py`

**问题**：在 `execute_code` 脚本中调用 `read_file()` 返回的内容带有行号前缀（格式 `"     1|import json"`），直接传给 `write_file()` 会把行号写入文件，污染内容。

**修复**：
- `tools/file_tools.py`：
  - 新增 `READ_FILE_RAW_SCHEMA` — 只接受 `path` 参数，无 offset/limit 分页
  - 新增 `_handle_read_file_raw()` handler — 调用 `file_ops.read_file_raw(path)` 返回纯文本
  - 注册 `registry.register(name="read_file_raw", ...)` 为新工具
- `tools/code_execution_tool.py`：
  - `SANDBOX_ALLOWED_TOOLS` 加 `read_file_raw`
  - `_TOOL_STUBS` 加 `read_file_raw` stub

**用法**（在 `execute_code` 中）：
```python
from hermes_tools import read_file_raw, write_file

content = read_file_raw("some.py")["content"]  # 纯文本，无行号
write_file("copy.py", content)                  # 安全写入
```

### 3.13 429 重试机制修复 — 禁用 SDK 内部重试 + 退避后 `continue`

**文件**：`agent/agent_runtime_helpers.py` / `agent/conversation_loop.py`

**问题**：两个问题叠加导致 429 重试异常：

1. **SDK 内部重试**（`agent_runtime_helpers.py`）：创建 OpenAI 客户端时 `max_retries` 使用 SDK 默认值 2，导致收到 429 后 SDK 先内部重试 2 次（近乎零延迟），然后才把最终错误抛给应用层。用户看到的是 3 次 429 瞬间发生。

2. **退避后缺少 `continue`**（`conversation_loop.py`）：应用层的退避等待（`jittered_backoff` + `sleep`）执行完后，代码没有 `continue` 回到 `while retry_count < max_retries:` 循环顶部，而是掉到 `if response is None: break`，只重试了 1 次就退出了循环。

**修复**：
- `agent_runtime_helpers.py`：创建客户端前加 `client_kwargs["max_retries"] = 0`，所有重试由应用层统一控制
- `conversation_loop.py`：退避 sleep 循环结束后加 `continue`，回到 while 循环顶部继续重试

**效果**：429 时按 `3s → 6s → 12s → 20s(封顶)` 递增退避重试，不会瞬间耗尽重试次数。

### 3.14 patch 工具缩进错乱修复 — 截断标记 + 守卫 + 行数校验

**文件**：`tools/file_operations.py` / `tools/fuzzy_match.py`

**问题**：三个问题叠加导致 patch 工具总是写错缩进：

1. `read_file` 的长行截断标记 `"... [truncated]"` 视觉上不醒目，模型容易误当作真实的文件内容，拷贝到 `old_string` 参数中
2. `fuzzy_find_and_replace` 的模糊匹配对含截断标记的垃圾输入容忍度过高（`context_aware` 策略 50% 行相似度即命中），匹配成功后把含 `...` 的乱码写回文件
3. `_reindent_replacement` 在 `file_region` 和 `old_string` 行数不一致时仍计算缩进差值，导致 if/else 块缩进全部偏移

**修复**：

- `tools/file_operations.py` — `_add_line_numbers()`：
  - 截断标记从 `"... [truncated]"` 改为 `" … [TRUNCATED]"`（楔形点 + 全大写），视觉上更醒目，模型不易误认为文件内容

- `tools/fuzzy_match.py` — `fuzzy_find_and_replace()`：
  - 在所有策略匹配前增加**截断标记守卫**：若 `old_string` 包含 `[TRUNCATED]`，直接返回错误，提示模型用 `read_file_raw` 重读
  - 在所有策略匹配后增加**内容相似度守卫 `_verify_match_content()`**：去除所有空白后对比 `old_string` 与匹配区域文本的相似度。防止 `context_aware` 等策略因关键词出现在错误位置而误匹配

- `tools/fuzzy_match.py` — `_reindent_replacement()`：
  - 缩进基准从"首行"改为**众数（mode）缩进**：LLM 生成的 old_string 首行缩进可能与块内其他行不同，用众数更稳健
  - 新增**缩进一致性保护**：非空行跨越 2 种以上不同的缩进宽度时跳过缩进重算，原样返回 `new_string`，避免因匹配错误导致缩进偏移
  - 保留原有的**行数不一致保护**：`old_string` 和 `file_region` 非空行数不一致时跳过

**效果**：五层防线：`read_file` 显示更醒目标记 → 模型不易误用 → 即使误用了，截断标记守卫在策略匹配前拒绝 → 即使漏网，内容相似度守卫拒绝无关区域的误匹配 → 即使再漏网，`_reindent_replacement` 用行数/缩进一致性双重兜底。patch 工具的缩进错误问题彻底解决。

**2026-06-08 更新**：`_reindent_replacement()` 增强缩进对齐逻辑。当 `old_string` 缩进与文件一致时，LLM 生成的 `new_string` 首行可能因 tool-call 序列化丢失了前导空白。新增检测：统计 `new_string` 非空行的众数缩进，若与 `old_string` 缩进不同，按文件实际缩进重新对齐。解决了 `old_string` 缩进匹配但 `new_string` 缩进偏移的特殊场景。

### 3.15 技能搜索工具 — `find_skills`

**文件**：`tools/skills_tool.py` / `agent/prompt_builder.py`

**问题**：原有的 `skills_list` 只列出所有技能名称和描述，没有任何搜索/过滤能力。模型每次都要手动扫描数百个已安装技能来找到合适的，效率极低，且经常忽略可用技能。

**修复**：
- `tools/skills_tool.py`：
  - 新增 `find_skills(query, limit)` 函数 — 关键词搜索 + 多维度评分排序（名称子串命中 = 100 分，关键词出现次数累加）
  - 返回 JSON 格式结果，含 `skills[]`、`categories[]`、`count`、`total_available`
  - 注册为新工具，工具集 `skills`
- `agent/prompt_builder.py`：
  - 技能系统提示从 "mandatory" 模式改为 "find on demand" 模式 — 不再列出所有技能，改为提示模型使用 `find_skills(query)` 按需搜索
  - 加入 `PROJECT_CONTEXT_GUIDANCE` 常量

### 3.16 推理内容回传优化

**文件**：`agent/agent_runtime_helpers.py`

**问题**：每轮对话都原样回传 `reasoning_content`（DeepSeek 的思考内容），存在三个问题：
1. 浪费 ~500 计费 tokens/轮
2. 破坏 DeepSeek 的字节级稳定前缀（prompt cache 依赖此机制）
3. 使每个请求膨胀，模型不需要重新读取这些思考文本

**修复**：
- `copy_reasoning_content_for_api()`：只在 `_needs_thinking_reasoning_pad()` 返回 True 的 Provider 才回传（DeepSeek / Kimi / MiMo thinking mode）
- 其他 Provider 直接从 API 消息体中删除 `reasoning_content`
- `reasoning_content` 为空字符串时自动填充 `" "`（DeepSeek V4 Pro 拒绝空字符串）

### 3.17 项目上下文文件按需加载

**文件**：`agent/system_prompt.py`、`agent/error_classifier.py` / `agent/prompt_builder.py`

**问题**：每次启动时自动扫描并加载 AGENTS.md/CLAUDE.md/.cursorrules/.hermes.md 文件。当仓库中存在这些文件时，每次轮询都注入 ~7000 tokens 的上下文，大量浪费。

**修复**：
- `agent/system_prompt.py`：取消自动加载项目上下文文件的逻辑，改为注入简短的 `PROJECT_CONTEXT_GUIDANCE` 提示
- 提示内容告知模型使用 `search_files()` 找到文件、`read_file()` 按需加载

### 3.18 insufficient_quota 重分类为 rate_limit

**文件**：`agent/error_classifier.py`

**问题**：`insufficient_quota` 被归类为 `FailoverReason.billing`（不可重试），导致凭证立即耗尽并触发凭据池轮换。但对 Alibaba/DashScope 等平台，每日额度用满返回 `insufficient_quota` 是临时性速率限制，等待后会自动恢复。把这种临时配额误判为永久计费耗尽会导致不必要的凭据切换。

**修复**：
- 在 `_classify_by_error_code()` 中，`insufficient_quota` 从 `billing` 组移出，新建单独的 `rate_limit` 分支
- 新增代码块将 `insufficient_quota` 映射为 `FailoverReason.rate_limit`（可重试）
- 凭据池自身的连续 429 检测（#11314）负责处理真正的计费耗尽场景
- 注释说明两段式策略：默认按 rate_limit 重试，凭据池兜底检测真实计费耗尽


### 3.19 CRLF 换行符归一化

**文件**：`tools/file_operations.py`

**问题**：Windows 上的文本文件使用 CRLF (\r\n) 作为换行符。
`read_file_raw` 和 `read_file` 从 shell 命令读取文件内容时原样保留了 CRLF，导致：
1. Python 的 `.split("\n")` 每行末尾残留 `\r` 字符，影响后续字符串操作
2. `patch` 工具(fuzzy matching)的缩进计算被不可见的 `\r` 干扰
3. `execute_code` 中用 `.replace()` 构造字符串时 LF 不匹配 CRLF

**修复**：
- `read_file_raw()`：在 `_strip_bom()` 后调用 `_normalize_line_endings(raw_content, "\n")` 做 CRLF -> LF 转换
- `read_file()`(分页模式)：同样在 `_strip_bom()` 后做 `_normalize_line_endings(read_output, "\n")`
- `write_file()`：已有 `_detect_file_line_ending()` + `_normalize_line_endings(content, "\r\n")` 逻辑，写入时自动 LF -> CRLF，此端未改动

**效果**：
- 读端：模型永远看到 LF 纯文本，无需关心物理换行符
- 写端：文件写入后保持 CRLF(Windows 原生格式)
- patch 工具：fuzzy matching 输入已归一化，`.split("\n")` 不再残留 `\r`，缩进计算准确

**验证**：
```bash
python -c "
from tools.file_operations import ShellFileOperations
from tools.terminal_tool import _active_environments
env = _active_environments.get('__default__')
ops = ShellFileOperations(env)
res = ops.read_file_raw('tools/file_operations.py')
print('CRLF in read result:', chr(13)+chr(10) in res.content)
"
```

---

## 4. 验证安装

```bash
cd hermes-agent

# 验证补丁已应用
python -c "compile(open('tools/browser_tool.py','rb').read(),'x','exec'); print('browser_tool.py OK')"
python -c "compile(open('tools/environments/local.py','rb').read(),'x','exec'); print('local.py OK')"
python -c "compile(open('hermes_logging.py','rb').read(),'x','exec'); print('hermes_logging.py OK')"

# 验证 CDP 后端
python -c "from tools.browser_cdp_backend import CdpClient; print('CDP backend OK')"

# 验证 read_file_raw 已注册
python -c "import tools.file_tools; from tools.registry import registry; print('read_file_raw OK' if registry.get_entry('read_file_raw') else 'MISSING')"

# 验证关键函数存在
grep -c "browser_cdp_backend" tools/browser_tool.py       # 应 > 0
grep -c "taskkill" tools/environments/local.py              # 应 > 0
grep -c "WSL" tools/environments/local.py                    # 应 > 0
grep -c "缓存" cli.py                                        # 应 > 0
grep -c "TRUNCATED" tools/file_operations.py                 # 应 > 0（截断标记已改为大写）
grep -c "Line-count guard" tools/fuzzy_match.py              # 应 > 0
grep -c "find_skills" tools/skills_tool.py                    # 应 > 0
grep -c "PROJECT_CONTEXT_GUIDANCE" agent/prompt_builder.py    # 应 > 0
grep -c "needs_thinking_reasoning_pad" agent/agent_runtime_helpers.py  # 应 > 0
grep -c "_normalize_line_endings" tools/file_operations.py          # 应 > 1（读和写各至少 1 处）
```

---

## 5. 补丁冲突处理

`git apply` 报错时说明上游代码结构已变。

### 步骤 1：尝试 3 路合并

```bash
git apply -3 --ignore-whitespace windows-patches/hermes-windows.patch
```

### 步骤 2：查看冲突位置

```bash
git apply windows-patches/hermes-windows.patch 2>&1 | head -20
```

### 步骤 3：按文件逐个手动修改

先修 `tools/browser_tool.py`（3 个修改点），再修 `tools/environments/local.py`（19 个修改点），以及其他文件（`agent/agent_runtime_helpers.py` / `agent/conversation_loop.py` / `agent/error_classifier.py` / `agent/prompt_builder.py` / `agent/system_prompt.py` / `agent/usage_pricing.py` / `cli.py` / `hermes_cli/main.py` / `hermes_cli/skin_engine.py` / `hermes_logging.py` / `hermes_cli/config.py` / `tools/file_tools.py` / `tools/code_execution_tool.py` / `tools/skills_tool.py`），参考第 3 节的说明。

### 步骤 4：校验语法

```bash
python -c "compile(open('tools/browser_tool.py','rb').read(),'tools/browser_tool.py','exec'); print('OK')"
python -c "compile(open('tools/environments/local.py','rb').read(),'tools/environments/local.py','exec'); print('OK')"
```

---

## 6. 回滚方法

```bash
git checkout -- tools/browser_tool.py tools/environments/local.py \
  hermes_logging.py hermes_cli/main.py cli.py \
  agent/conversation_loop.py agent/usage_pricing.py hermes_cli/skin_engine.py \
  hermes_cli/config.py agent/agent_runtime_helpers.py \
  tools/file_tools.py tools/code_execution_tool.py \
  tools/file_operations.py tools/fuzzy_match.py \
    agent/error_classifier.py \
    README.zh-CN.md
rm -f tools/browser_cdp_backend.py
```

或直接 `git checkout .` 恢复所有文件。

---

## 7. 文件清单

| 文件 | 作用 |
|------|------|
| `windows-patches/hermes-windows.patch` | `git apply` 补丁，修改 21 个源文件 |
| `windows-patches/browser_cdp_backend.py` | Python CDP 浏览器后端，复制到 `tools/` |
| `windows-patches/WINDOWS_COMPAT.md` | 本文档 |

修改的 21 个源文件：`agent/agent_runtime_helpers.py`、`agent/conversation_loop.py`、`agent/prompt_builder.py`、`agent/system_prompt.py`、`agent/usage_pricing.py`、`cli.py`、`hermes_cli/config.py`、`hermes_cli/main.py`、`hermes_cli/skin_engine.py`、`hermes_logging.py`、`tests/agent/test_prompt_builder.py`、`tests/agent/test_usage_pricing.py`、`tools/browser_tool.py`、`tools/code_execution_tool.py`、`tools/environments/local.py`、`tools/file_operations.py`、`tools/file_tools.py`、`tools/fuzzy_match.py`、`tools/skills_tool.py`，以及新增 `tools/browser_cdp_backend.py`。另外含 `README.zh-CN.md` 中文文档更新。