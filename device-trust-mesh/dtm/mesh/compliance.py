"""Ray Serve entry point for the Compliance agent: ``dtm.mesh.compliance:app``."""
from dtm.mesh.agents.compliance import ComplianceAgent as Agent
from dtm.mesh.agents.host import agent_deployment


def make(bus, context, llm) -> Agent:
    return Agent(bus, context, llm)


ComplianceAgent = agent_deployment("ComplianceAgent", "compliance", make)
app = ComplianceAgent.bind()
