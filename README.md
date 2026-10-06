# Insurance Claim Case Summarization Agent

AI agent for summarizing insurance claim cases, built with Agentic Star.

> **Category**: Cat 2 (domain-specific pipeline)
> **Industry**: Insurance
> **Template ID**: INS-C2-004

## Overview

Turns a multi-document insurance claim file into one structured, triage-ready case summary.
A claim arrives as a JSON case: a claim reference, claim type, date of loss and a list of
documents (First Notice of Loss, medical reports, adjuster notes, repair estimates, photo
captions). The agent normalises and types every document, groups them, and writes six fixed
sections — case overview, incident facts, documentation summary, injury/medical, damages
assessment and recommended action — so a handler can triage in seconds instead of reading the
whole file.

Two things make the output safe to hand on. Policyholder personal data (emails, phone numbers,
national IDs, payment-card and social-security-style numbers) is masked in the generated
sections and counted in a redaction record, and the finished document passes an output boundary
that withholds it entirely on a credential leak and expresses every monetary figure on a fixed
1,000 grid.

Callers may pass structured damages data alongside the documents (`input_context`: deductible,
policy limit, reserve, and labelled estimate lines). Every field is bounds-checked; the agent
then computes real aggregates — estimated total, net exposure, reserve variance — classifies
exposure severity, and lets that severity drive the recommended action. Line-item amounts are
never rendered; only the aggregates are. Without that data the agent degrades to the summary it
can derive from the documents alone.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at
graph compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, graphs, schemas)
tests/        unit and boundary tests
config/       agent manifest and runtime parameters
prompts/      system prompt template for a live-LLM build
docs/         design and test documentation
```

See `docs/` for the design specification and the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the document typing rules, redaction patterns and severity bands with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
