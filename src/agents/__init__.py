"""Local role contracts, tool registration, and default personas."""

from src.agents.base_agent import AgentError, AgentReply, AgentResult, BaseAgent, Tool, ToolCall, ToolResult
from src.agents.personas import PlannerAgent, ResearchAgent, VerificationAgent, default_agents

__all__ = ["AgentError", "AgentReply", "AgentResult", "BaseAgent", "Tool", "ToolCall", "ToolResult",
           "PlannerAgent", "ResearchAgent", "VerificationAgent", "default_agents"]
