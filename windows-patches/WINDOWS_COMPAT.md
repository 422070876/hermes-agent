# Hermes Agent Windows 兼容指南

> 让 Hermes Agent 在 Windows 上（Git Bash / WSL）正常工作的补丁包。

| 项目 | 值 |
|------|-----|
| **基于上游版本** | [v2026.6.19](https://github.com/NousResearch/hermes-agent) — commit `5ff11a689`（2026-06-21） |
| **补丁更新日期** | 2026-06-22 |
| **补丁修改文件** | 28 个源文件（27 个修改 + 1 个新增 README.zh-CN.md 文档） |
| **新增伴侣文件** | `tools/browser_cdp_backend.py`（475 行，Python CDP 浏览器后端） |
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
git checkout 5ff11a689  # Hermes Agent v2026.6.19

# 下载补丁包
mkdir -p windows-patches
curl -Lo windows-patches/hermes-windows.patch \
  https://raw.githubusercontent.com/422070876/hermes-agent/main/windows-patches/hermes-windows.patch
curl -Lo windows-patches/browser_cdp_backend.py \
  https://raw.githubusercontent.com/422070876/hermes-agent/main/windows-patches/browser_cdp_backend.py

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
| 3 | **PATH 分隔符错误** | 硬编码 Unix `:`，Windows 需要用 `;` | 平台感知的 `_PATH_SEP` + `os.pathsep` |
| 4 | **HOME 缺失** | 没有设置 HOME 变量，bash 行为异常 | 从 USERPROFILE 自动推导 |
| 5 | **临时目录不兼容** | `tempfile.gettempdir()` 返回带空格的路径 | 用 `HERMES_HOME/cache/terminal` |
| 6 | **进程杀死不彻底** | `os.killpg` 在 Windows 上不存在 | `taskkill /T /F` + PowerShell 清理 Chrome |
| 7 | **日志轮转权限错误** | `PermissionError` 日志轮转失败 | 用 `shutil.copy2` + 清空替代 rename |
| 8 | **启动工作目录错误** | 终端工具 CWD 默认不在 Hermes 家目录 | 启动时 chdir 到 `get_hermes_home()` |
| 9 | **API 费用显示人民币** | 官方 DeepSeek 定价为 USD，国内用户看 ¥ 方便 | 所有 DeepSeek 定价改为 CNY，状态栏显示 ¥ |
| 10 | **限流回退过慢** | 被限流时也用默认 60s 最大等待 | 限流时 base_delay=3.0, max_delay=20 快速恢复 |
| 11 | **自定义定价配置入口** | 使用本地模型或国内 API 需手动改代码覆盖定价 | `config.yaml` 中加 `custom_pricing` 字段，运行时动态加载 |
| 12 | **execute_code 行号污染** | `read_file()` 返回内容带行号前缀 | 新增 `read_file_raw()` 工具，返回纯文本内容 |
| 13 | **429 重试间隔太短** | SDK 默认重试 2 次零延迟 | 禁用 SDK 内部重试 + 退避后 `continue` |
| 14 | **patch 工具缩进错乱** | `read_file` 截断标记被模型误作文件内容传入 patch，模糊匹配后缩进算歪 | 截断标记大写 + 守卫 + 众数缩进对齐 + 一致性保护 |
| 15 | **技能搜索能力缺失** | `skills_list` 只能列出所有技能名，大模型需要手动扫描数百个 | 新增 `find_skills(query)` 工具 + 评分排序 |
| 16 | **推理内容回传浪费 tokens** | 每轮回传 `reasoning_content` 消耗 ~500 tokens | 只在需要 thinking pad 的 Provider 回传 |
| 17 | **项目上下文文件过度加载** | AGENTS.md/CLAUDE.md 自动加载浪费 ~7000 tokens/轮 | 改为提示模型按需用 `read_file()` 加载 |
| 18 | **insufficient_quota 误判 billing** | 误为永久计费耗尽导致凭证池轮换 | 重分类为 rate_limit（可重试） |
| 19 | **CRLF 换行符干扰** | `read_file_raw` 返回带 CRLF 内容 | 读入时 CRLF→LF 归一化，写回时 LF→CRLF |
|| 20 | **simple_mode 配置模式** | 无头部署需要禁用并行工具/子代理 | 通过 `simple_mode` 参数 + CLI `--simple-mode` 控制 |

---

### 3.1 CDP 浏览器后端

**文件**：`tools/browser_tool.py`

**问题**：`agent-browser` CLI 的 Rust daemon 通过 CDP 操控 Chrome，Windows 上 WebSocket 阻塞超时。

**修复**：
1. Windows 上调用 `browser_cdp_backend.start_chrome_and_get_cdp_url()`
2. 通过 Python CDP 客户端路由命令
3. CDP 命令分发函数支持 open/snapshot/click/type/scroll/back/press/console/screenshot/get_images/eval/close

### 3.2 浏览器后端 ES5 兼容

**文件**：`tools/browser_tool.py`

`browser_get_images()` JS 代码改为 ES5 IIFE，兼容两种后端返回格式。

### 3.3 项目上下文文件按需加载

**文件**：`agent/system_prompt.py`

不再自动加载 AGENTS.md/CLAUDE.md/.cursorrules，改为注入简短提示让模型用 `search_files()`/`read_file()` 按需加载。节省 ~7000 tokens/轮。

### 3.4 simple_mode delegate_task 守卫

**文件**：`agent/tool_executor.py`

`simple_mode=True` 时 delegate_task 直接返回禁用提示，不启动 spinner/子代理。

### 3.5 自定义定价配置入口

**文件**：`hermes_cli/config.py`

`DEFAULT_CONFIG` 新增 `custom_pricing: {}`，支持用户通过 config.yaml 覆盖模型定价。

### 3.6 README 中文文档更新

**文件**：`README.zh-CN.md`

Windows 安装说明指向 `windows-patches` 和 `WINDOWS_COMPAT.md`。

### 3.7 测试适配

**文件**：`tests/agent/test_prompt_builder.py`

技能系统提示格式变更后更新断言。

---

## 4. 验证安装

```bash
cd hermes-agent

# 验证 CDP 后端
python -c "from tools.browser_cdp_backend import CdpClient; print('CDP backend OK')"

# 验证 C 关键函数存在
grep -c "browser_cdp_backend" tools/browser_tool.py                    # 应 > 0
grep -c "taskkill" tools/environments/local.py                          # 应 > 0
grep -c "PROJECT_CONTEXT_GUIDANCE" agent/system_prompt.py               # 应 > 0
grep -c "read_file_raw" tools/file_tools.py                             # 应 > 0
grep -c "find_skills" tools/skills_tool.py                              # 应 > 0
grep -c "simple_mode" run_agent.py                                      # 应 > 0
grep -c "_simple_mode" agent/tool_executor.py                           # 应 > 0
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

参考第 3 节的说明。

---

## 6. 回滚方法

```bash
git checkout -- agent/agent_init.py agent/agent_runtime_helpers.py \
agent/chat_completion_helpers.py agent/conversation_loop.py \
agent/error_classifier.py agent/prompt_builder.py agent/system_prompt.py \
agent/tool_executor.py agent/usage_pricing.py cli.py \
hermes_cli/_parser.py hermes_cli/config.py hermes_cli/main.py \
hermes_cli/skin_engine.py hermes_logging.py run_agent.py \
tests/agent/test_prompt_builder.py tests/agent/test_usage_pricing.py \
tests/tools/test_fuzzy_match.py tools/browser_tool.py \
tools/code_execution_tool.py tools/environments/local.py \
tools/file_operations.py tools/file_tools.py tools/fuzzy_match.py \
tools/skill_manager_tool.py tools/skills_tool.py README.zh-CN.md
rm -f tools/browser_cdp_backend.py
```

或直接 `git checkout .` 恢复所有文件。

---

## 7. 文件清单

| 文件 | 作用 |
|------|------|
| `windows-patches/hermes-windows.patch` | `git apply` 补丁，修改 28 个源文件 |
| `windows-patches/browser_cdp_backend.py` | Python CDP 浏览器后端，复制到 `tools/` |
| `windows-patches/WINDOWS_COMPAT.md` | 本文档 |

修改的 28 个源文件：`agent/agent_init.py`、`agent/agent_runtime_helpers.py`、`agent/chat_completion_helpers.py`、`agent/conversation_loop.py`、`agent/error_classifier.py`、`agent/prompt_builder.py`、`agent/system_prompt.py`、`agent/tool_executor.py`、`agent/usage_pricing.py`、`cli.py`、`hermes_cli/_parser.py`、`hermes_cli/config.py`、`hermes_cli/main.py`、`hermes_cli/skin_engine.py`、`hermes_logging.py`、`run_agent.py`、`tests/agent/test_prompt_builder.py`、`tests/agent/test_usage_pricing.py`、`tests/tools/test_fuzzy_match.py`、`tools/browser_tool.py`、`tools/code_execution_tool.py`、`tools/environments/local.py`、`tools/file_operations.py`、`tools/file_tools.py`、`tools/fuzzy_match.py`、`tools/skill_manager_tool.py`、`tools/skills_tool.py`，以及新增 `tools/browser_cdp_backend.py`。另外含 `README.zh-CN.md` 中文文档更新。