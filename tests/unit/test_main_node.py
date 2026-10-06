# INS-C2-004 — Unit Tests: Main Node

from src.nodes.main_node import MainNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel


class TestMainNode:
    """Unit tests for the retained MainNode stub.

    Nodes are invoked through BaseNode.__call__ (`node(state)`) — NOT execute()
    directly — so the framework trust gate runs on every unit invocation.
    MainNode is ANONYMOUS.
    """

    def setup_method(self):
        self.node = MainNode()

    def test_success_path(self):
        """The retained stub processes valid input and returns SUCCESS."""
        state = {
            "validated_input": "test input",
            "node_history": [],
            "error_log": [],
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["result"] is not None

    def test_empty_input(self):
        """The retained stub handles empty input gracefully."""
        state = {
            "validated_input": "",
            "node_history": [],
            "error_log": [],
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_execute_method_signature(self):
        """A node must implement execute(state), never _invoke_impl.

        Canonical node contract:
          - Override: execute(self, state: AgentState) -> dict
          - PROHIBITED: _invoke_impl(), process() override
        """
        import inspect

        # Must have execute() defined on the concrete class (not just inherited stub)
        assert hasattr(MainNode, "execute"), "MainNode must implement execute()"

        sig = inspect.signature(MainNode.execute)
        params = list(sig.parameters.keys())
        # execute(self, state) — at minimum two parameters
        assert len(params) >= 2, f"execute() must accept (self, state), got params: {params}"
        assert params[1] == "state", f"Second parameter must be 'state', got '{params[1]}'"

        # Must NOT define _invoke_impl at the domain level
        assert (
            "_invoke_impl" not in MainNode.__dict__
        ), "_invoke_impl() must not be defined in MainNode — use execute() instead"
