# 🚀 The Logan Chaos: Autonomous Edge Codebase Resilience Engine

> **Track:** Best Use of Gemma 4 — MLH Hackathon at Vishwakarma University  
> **Repository:** [https://github.com/Manthan-J-06/the-logan-chaos](https://github.com/Manthan-J-06/the-logan-chaos)  
> **Core Stack:** Local Gemma 4 (`gemma4:e2b`) via Ollama, Graphify, NetworkX, Pytest, Rich, Vis.js

---

## ⚡ Key Architectural Metrics

* **Zero Cloud Dependencies:** 100% private, on-device edge inference powered by local **Gemma 4** over Ollama.
* **302 AST Nodes & 775 Graph Edges:** Hyper-dense Abstract Syntax Tree mapping generated via **Graphify**.
* **103/103 Integration Tests:** Strict contract test suite covering cryptography, RBAC matrices, route context dispatchers, rate limiters, and telemetry sinks.
* **2-Turn Convergence Budget:** Low-latency parallel batch remediation repairing multi-module failures in under 2 reflection cycles.

---

## 🏛️ System Architecture

```text
       ┌────────────────────────────────────────────────────────┐
       │             Graphify AST Topology Engine               │
       │     (302 Nodes, 775 Edges, Betweenness Centrality)     │
       └──────────────────────────┬─────────────────────────────┘
                                  │
                 Identifies Topological Cut Vertices
                                  ▼
       ┌────────────────────────────────────────────────────────┐
       │             Berserker (Red-Team Agent)                 │
       │       Gemma 4 Semantic Contract Sabotage               │
       └──────────────────────────┬─────────────────────────────┘
                                  │
                   Injects Dynamic Fault Mutations
                                  ▼
       ┌────────────────────────────────────────────────────────┐
       │             Sandbox Microservice (103 Tests)           │
       │      config.py | telemetry.py | auth.py | router.py    │
       │                        core.py                         │
       └──────────────────────────┬─────────────────────────────┘
                                  │
                   Pytest Failures & Assertion Tracebacks
                                  ▼
       ┌────────────────────────────────────────────────────────┐
       │             Logan (Blue-Team Resilience)               │
       │  • Neural Trace Telemetry (Streaming Gemma 4 Thoughts) │
       │  • Parallel Batch Remediation (2-Turn Budget)          │
       │  • 100% Green Reconvergence (Topological Invariance)   │
       └────────────────────────────────────────────────────────┘


⚙️ How It Works

[ 1. Graphify AST ] ──► [ 2. Berserker (Red Team) ]
   (Extracts 302 Nodes)      (Gemma 4 Injects Faults)
                                      │
                                      ▼
[ 4. 100% Green Pass ] ◄── [ 3. Logan (Blue Team) ]
  (103 Tests Restored)       (Streams Thoughts & Heals)
  
Ingest & Profile: graphify sandbox/ builds an interactive knowledge graph and identifies single points of failure (core.py, router.py).

Adversarial Chaos: Berserker prompts Gemma 4 to inject semantic contract breaks (e.g., dropping routes, severing RBAC auth).

Cognitive Remediation: Logan captures failure tracebacks, streams Gemma 4's diagnostic deductions live, and applies surgical AST patches.

Reconvergence: Within 2 cycles, all 103 integration tests return to green with zero human intervention.


🚀 Quickstart
1. Prerequisites
Bash
# Serve Gemma 4 locally
ollama run gemma4:e2b
2. Install & Run
Bash
git clone [https://github.com/Manthan-J-06/the-logan-chaos.git]
cd the-logan-chaos
pip install rich networkx matplotlib pytest requests graphify

# Run the resilience engine
python main.py
3. View the Graphify Web Topology
Bash
open sandbox/graphify-out/graph.html


🛠️ Tech Stack
LLM Engine: Gemma 4 (gemma4:e2b) via Ollama

Graph Analysis: Graphify, NetworkX

Validation & UI: Pytest, Rich, Matplotlib, Vis.js
