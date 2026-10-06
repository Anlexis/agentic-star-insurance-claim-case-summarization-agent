"""AGENTIC STAR Marketplace entrypoint — one-shot Pod process.

Executed by the Marketplace image as its `CMD`. Compiles the agent,
provisions its secrets, then hands off to shared.bootstrap.marketplace_app
for the Marketplace lifecycle (identity, input, events, terminal delivery,
exit). Mirrors agentcore's own `agents/base/chat_agent/cli.py` (the pattern
this file was copied from).

`namespace=` here is the Marketplace secret-provisioning namespace — a different
concept from `config/agent.yaml`'s AgentRegistry `namespace:` key that happens to
share its value. Mirrors
`src/api/server.py`'s existing `secrets_factory(namespace="ins-c2-004",
agent_name="InsuranceClaimCaseSummarizationAgent")` call shape rather than a per-template value: one
Marketplace Pod deploys exactly one template, so there is no cross-template
secret-path collision to guard against, and the namespace value is set at
scaffold init the same way `server.py` already expects it to be.
"""

from pathlib import Path

from framework.utils.config_loader import load_agent_config
from shared.bootstrap.marketplace_app import run_agent_marketplace
from src.graph.graph import InsuranceClaimCaseSummarizationAgent

# Add config overrides here to set values without touching config/config.yaml.
extend_config = {}

if __name__ == "__main__":
    run_agent_marketplace(
        InsuranceClaimCaseSummarizationAgent,
        agent_name="InsuranceClaimCaseSummarizationAgent",
        namespace="ins-c2-004",
        config={**load_agent_config(Path(__file__).resolve().parent), **extend_config},
    )
