"""Ray Serve entry point for the Enrolment agent: ``dtm.mesh.enrolment:app``."""
from dtm.mesh.agents.enrolment import EnrolmentAgent as Agent
from dtm.mesh.agents.host import agent_deployment


def make(bus, context, llm) -> Agent:
    return Agent(bus, context, llm)


EnrolmentAgent = agent_deployment("EnrolmentAgent", "enrolment", make)
app = EnrolmentAgent.bind()
