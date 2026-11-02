"""Default logical roles; all Python executes on the coordinator."""

from src.agents.base_agent import BaseAgent


class PlannerAgent(BaseAgent):
    def __init__(self, client, **kwargs):
        super().__init__(client, agent_id="planner", node="node1", **kwargs)

    @property
    def role_prompt(self):
        return "You are the planner. Plan the next step from the task and current facts. Distinguish proposals from facts. Suggest a concise next_subgoal."


class ResearchAgent(BaseAgent):
    def __init__(self, client, **kwargs):
        super().__init__(client, agent_id="researcher", node="node2", **kwargs)

    @property
    def role_prompt(self):
        return "You are the researcher. Investigate supplied local evidence and current graph memory. State supported facts. Do not claim external searches."


class VerificationAgent(BaseAgent):
    can_complete = True

    def __init__(self, client, **kwargs):
        super().__init__(client, agent_id="verifier", node="node1", **kwargs)

    @property
    def role_prompt(self):
        return "You are the verifier. Check the task against current facts, state supported conclusions and flag gaps. Set complete true only when the task is answered."


def default_agents(client):
    return (PlannerAgent(client), ResearchAgent(client), VerificationAgent(client))
