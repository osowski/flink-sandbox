"""Ray Serve entry point for the Risk agent: ``dtm.mesh.risk:app``."""
from dtm.mesh.agents.host import agent_deployment
from dtm.mesh.agents.risk import RiskAgent as Agent


def make(bus, context, llm) -> Agent:
    return Agent(bus, context, llm)


RiskAgent = agent_deployment("RiskAgent", "risk", make)
app = RiskAgent.bind()
