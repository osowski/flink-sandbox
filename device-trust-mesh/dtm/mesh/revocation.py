"""Ray Serve entry point for the Revocation agent: ``dtm.mesh.revocation:app``."""
from dtm.mesh.agents.host import agent_deployment
from dtm.mesh.agents.revocation import RevocationAgent as Agent


def make(bus, context, llm) -> Agent:
    return Agent(bus, context, llm)


RevocationAgent = agent_deployment("RevocationAgent", "revocation", make)
app = RevocationAgent.bind()
