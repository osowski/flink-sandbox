"""Ray Serve entry point for the Step-up agent: ``dtm.mesh.step_up:app``."""
from dtm.mesh.agents.host import agent_deployment
from dtm.mesh.agents.step_up import StepUpAgent as Agent


def make(bus, context, llm) -> Agent:
    return Agent(bus, context, llm)


StepUpAgent = agent_deployment("StepUpAgent", "step-up", make)
app = StepUpAgent.bind()
