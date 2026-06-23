#!/usr/bin/env python3
"""
SimpleHermesAgent — 单进程单线程、无子Agent 的 Hermes Agent 入口。

在标准 AIAgent 基础上加了一个 ``simple_mode`` 开关，关闭全部多线程路径：
  - 不 spawn threading.Thread 做 API 调用（直接在当前线程执行）
  - 不启动 spinner 动画线程
  - 不启动 OpenRouter 预暖线程
  - 禁用 delegate_task 工具集（无子Agent）
  - sudo 密码/危险命令审批 callback 设为 auto-skip（无交互线程）

使用方式::

    from hermes_simple import SimpleHermesAgent

    agent = SimpleHermesAgent(
        base_url="http://localhost:30000/v1",
        model="deepseek-v4-flash",
    )

    # 多轮对话
    r1 = agent.run("你好")
    r2 = agent.run("我刚才说了什么？")  # 有上下文
    agent.reset()                      # 清空历史
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SimpleHermesAgent:
    """单进程单线程、无子Agent 的 Hermes Agent，支持多轮对话。

    Args:
        base_url: LLM API 地址（例如 http://localhost:30000/v1）
        model: 模型名称
        api_key: API key（可选，某些 provider 需要）
        simple_mode: 是否启用单线程模式（默认 True）
        provider: provider 标识
        max_iterations: 最大工具调用轮数
        verbose_logging: 是否打印详细日志
        **kwargs: 透传给 AIAgent 的其他参数
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        simple_mode: bool = True,
        provider: Optional[str] = None,
        max_iterations: int = 90,
        verbose_logging: bool = True,
        **kwargs: Any,
    ):
        self._conversation_history: Optional[List[Dict[str, Any]]] = None

        from run_agent import AIAgent

        # 设置审批/sudo callback 为 auto-skip，避免需要交互线程
        from tools.terminal_tool import set_approval_callback, set_sudo_password_callback

        def _auto_skip(command: str, description: str, **kw: Any) -> str:
            logger.info("SimpleHermesAgent auto-skipped: %s (%s)", command, description)
            return "deny"

        set_approval_callback(_auto_skip)
        set_sudo_password_callback(lambda: "")

        self._agent = AIAgent(
            base_url=base_url,
            model=model,
            api_key=api_key,
            provider=provider,
            simple_mode=simple_mode,
            quiet_mode=simple_mode,
            max_iterations=max_iterations,
            verbose_logging=verbose_logging,
            disabled_toolsets=["delegation"],
            **kwargs,
        )

    @property
    def agent(self) -> Any:
        """访问底层的 AIAgent 实例（高级用法用）。"""
        return self._agent

    @property
    def conversation_history(self) -> Optional[List[Dict[str, Any]]]:
        """当前对话历史。"""
        return self._conversation_history

    def run(self, message: str) -> str:
        """执行一轮对话，保留 history 支持多轮。

        Args:
            message: 用户消息

        Returns:
            str: 助手的最终回复文本
        """
        result = self._agent.run_conversation(
            user_message=message,
            conversation_history=self._conversation_history,
        )
        self._conversation_history = result.get("messages", [])
        final = result.get("final_response", "")
        return final or result.get("error", "")

    def reset(self) -> None:
        """清空对话历史，开始新会话。"""
        self._conversation_history = None

    def __repr__(self) -> str:
        model = getattr(self._agent, "model", "?")
        simple = getattr(self._agent, "_simple_mode", False)
        return f"<SimpleHermesAgent model={model!r} simple_mode={simple}>"