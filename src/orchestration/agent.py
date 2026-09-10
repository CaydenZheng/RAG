"""PocketFlow adapter retained only for Agent session reset."""

from pocketflow import Flow, Node


class AgentResetNode(Node):
    """Clear one validated, caller-scoped Agent session."""

    def prep(self, shared: dict):
        return shared.get("session_id", "default")

    def exec(self, session_id: str) -> dict:
        from src.agent.harness import agent_harness

        existed = agent_harness.reset_session(session_id)
        return {
            "status": "ok",
            "message": f"Session {session_id} reset.",
            "session_found": existed,
        }

    def post(self, shared: dict, prep_res, exec_res: dict) -> str:
        shared["answer"] = "会话已重置。"
        shared["tool_calls"] = []
        shared["session_found"] = exec_res["session_found"]
        return "default"


def create_agent_reset_flow() -> Flow:
    return Flow(start=AgentResetNode())


_agent_reset_flow = None


def get_agent_reset_flow() -> Flow:
    global _agent_reset_flow
    if _agent_reset_flow is None:
        _agent_reset_flow = create_agent_reset_flow()
    return _agent_reset_flow
