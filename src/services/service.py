"""AgentCore Platform v1.0"""

# Service layer: domain queries, external API wrappers, data aggregation.
# Must NOT contain business logic, routing, or credentials.
# Nodes call this; this calls shared/services/ for external integrations.
#
# This build assembles claim context deterministically inside BuildContextNode
# (src/nodes/build_context_node.py) and calls no external claims system, so no
# node invokes Service.fetch() on the runtime path. The module is the declared
# seam for a future external-integration contract: fetch() raises
# NotImplementedError by design, so a premature call fails loudly rather than
# silently returning empty data.

from __future__ import annotations

from typing import Any


class Service:
    """Domain service — the external-integration seam (see module docstring).

    fetch() is an intentional, unimplemented stub: no node calls it, and it
    raises NotImplementedError by design. Wire a real claims-system
    integration in here.
    """

    async def fetch(self, query: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch domain data for the given query.

        Intentional stub — raises NotImplementedError by design. Context is
        assembled in BuildContextNode; no runtime path calls this.
        """
        raise NotImplementedError("Service.fetch() is an unimplemented integration seam")
