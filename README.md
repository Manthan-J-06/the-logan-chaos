# The Logan Chaos: Autonomous Edge Codebase Resilience Engine

> Built for the **MLH Track: Best Use of Gemma 4** at Vishwakarma University.

An autonomous, multi-agent adversarial self-healing codebase resilience engine running locally on Apple Silicon edge hardware using **Gemma 4 (`gemma4:e2b`)** over Ollama.

---

## Technical Architecture & Core Pillars

### 1. Gemma-vs-Gemma Red/Blue Agentic Loop
* **Berserker (Adversarial Agent):** Prompts local Gemma 4 with an adversarial persona to inject semantic contract violations into high-centrality AST nodes.
* **Logan (Resilience Engine):** Observes pytest execution failures, streams cognitive reasoning tokens live, synthesizes surgical AST patches, and verifies systemic convergence.

### 2. Hyper-Dense AST Dependency Topology (302 Nodes, 307 Edges)
* Analyzes a 5-module enterprise-grade Python microservice using NetworkX and AST parsing.
* Uses **Betweenness Centrality** on undirected graph projections to mathematically identify topological Cut Vertices and architectural articulation points.

### 3. Cognitive Telemetry ("Logan Neural Trace")
* Consumes Gemma 4's native thought tokens in real-time to render a rich live terminal stream before synthesizing executable patches.

### 4. Multi-Turn Reflective Convergence Loop
* Iterative self-correction across up to 4 cycles. If a patch fails `pytest`, Logan ingests tracebacks, reflects on regressions, and re-patches until 100% of the 103 integration tests pass.

### 5. Multi-Node & Systemic Catastrophe Injection
* Dynamic targeting of individual modules (`core.py`, `auth.py`, `router.py`) or full multi-tier cascading failure (`WHOLE PROJECT`).

---

## Codebase Structure

```text
the-logan-chaos/
├── sandbox/              # 302-Node Enterprise-Grade Mock Microservice
│   ├── config.py         # Schemas, custom exceptions, policies, and factories
│   ├── telemetry.py      # Decorators (circuit breaker, latency, audit) and sinks
│   ├── auth.py           # JWT/API-key validation, rate limiting, RBAC matrix
│   ├── router.py         # Payload models, route context, RequestDispatcher
│   └── core.py           # GatewayEngine, pipeline execution, Cut Vertex
├── tests/
│   └── test_sandbox.py   # 103-test contract verification suite
├── berserker.py          # Adversarial Gemma 4 red-team mutator
├── healer.py             # Reflective Gemma 4 resilience & healing engine
└── main.py               # Unified CLI runner and Fruchterman-Reingold visualizer
