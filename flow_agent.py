"""
Agent Flow 编排 — 用 PocketFlow 包装 AgentHarness，与现有 RAG Flow 并存。
"""

from pocketflow import Flow, Node
from loguru import logger


class AgentNode(Node):
    """
    Agent 节点 — 包装 AgentHarness 为 PocketFlow Node。

    prep: 从 shared store 读 session_id + user_message
    exec: 调用 agent_harness.run()
    post: 将 answer/tool_calls/metrics 写回 shared store
    """

    def prep(self, shared: dict):
        session_id = shared.get("session_id", "default")
        user_message = shared.get("user_message", "")
        return session_id, user_message

    def exec(self, inputs: tuple) -> dict:
        session_id, user_message = inputs

        from src.agent.harness import agent_harness
        logger.info("Agent processing: session={}, msg={:.80}",
                     session_id, user_message)

        response = agent_harness.run(session_id, user_message)

        return {
            "answer": response.answer,
            "tool_calls": response.tool_calls,
            "iterations": response.iterations,
            "total_latency_ms": response.total_latency_ms,
            "error": response.error,
        }

    def post(self, shared: dict, prep_res, exec_res: dict) -> str:
        shared["answer"] = exec_res["answer"]
        shared["tool_calls"] = exec_res["tool_calls"]
        shared["iterations"] = exec_res["iterations"]
        shared["total_latency_ms"] = exec_res["total_latency_ms"]
        if exec_res.get("error"):
            shared["agent_error"] = exec_res["error"]

        logger.info("Agent finished: iter={} tools={} latency={:.1f}ms",
                     exec_res["iterations"], len(exec_res["tool_calls"]),
                     exec_res["total_latency_ms"])
        return "default"


class AgentResetNode(Node):
    """
    重置会话节点 — /new 命令。
    """

    def prep(self, shared: dict):
        return shared.get("session_id", "default")

    def exec(self, session_id: str) -> dict:
        from src.agent.harness import agent_harness
        agent_harness.reset_session(session_id)
        return {"status": "ok", "message": f"Session {session_id} reset."}

    def post(self, shared: dict, prep_res, exec_res: dict) -> str:
        shared["answer"] = "会话已重置。"
        shared["tool_calls"] = []
        return "default"


# ================================================================
# Agent Flow 构造
# ================================================================

def create_agent_flow() -> Flow:
    """创建 Agent Flow"""
    agent_node = AgentNode()
    return Flow(start=agent_node)


def create_agent_reset_flow() -> Flow:
    """创建会话重置 Flow"""
    reset_node = AgentResetNode()
    return Flow(start=reset_node)


# ================================================================
# 延迟加载单例
# ================================================================

_agent_flow = None
_agent_reset_flow = None


def get_agent_flow() -> Flow:
    global _agent_flow
    if _agent_flow is None:
        _agent_flow = create_agent_flow()
    return _agent_flow


def get_agent_reset_flow() -> Flow:
    global _agent_reset_flow
    if _agent_reset_flow is None:
        _agent_reset_flow = create_agent_reset_flow()
    return _agent_reset_flow
