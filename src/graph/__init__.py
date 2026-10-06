"""AgentCore Platform v1.0"""

# Registry entry-point export. config/agent.yaml points at
# `src.graph.graph.InsuranceClaimCaseSummarizationAgent`, and a registry that
# resolves the manifest by importing the package must find the class here —
# so the package root re-exports it, together with the `Graph` alias the
# standalone server imports.
from src.graph.graph import Graph, InsuranceClaimCaseSummarizationAgent

__all__ = ["InsuranceClaimCaseSummarizationAgent", "Graph"]
