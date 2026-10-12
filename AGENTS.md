# Project instructions for LLM agents

## Codebase navigation — consult graphify first

This repo maintains a code knowledge graph in `graphify-out/` (rebuilt 2026-09-30:
~6,900 nodes, ~14,000 edges, community hubs map every module and service, e.g.
"Data Source Guard API", "mStock Broker Integration", "Paper Runner").

Before exploring the codebase for any of these, use the graph instead of grepping
source cold:

- Architecture or "how does the system fit together" questions
- Cross-module / service-relationship questions ("what calls X", "which services
  touch mStock", "where does data flow from feed to runner")
- Locating code when the exact file or symbol is not already known
- Impact analysis before touching shared code (feeds, sessions, brokers, portfolio)

Entry points:

- `graphify-out/GRAPH_REPORT.md` — summary, community hubs, navigation
- `graphify-out/graph.html` — interactive linking chart of code and services
- `graphify-out/graph.json` + `manifest.json` — machine-readable graph and per-file
  hashes/mtimes (manifest also lets you check whether the graph is stale for a file)
- Prefer the `graphify` skill when available; it handles queries and rebuilds.

Fall back to direct Grep/Read when:

- The target file or symbol is already known precisely
- The graph lacks the detail needed, or `manifest.json` shows the file changed
  after the last build
- Verifying current ground truth before acting (the graph is a map, not the source)

Keeping it fresh: after major merges, refactors, or new services, rebuild the graph
(via the graphify skill) so `GRAPH_REPORT.md` and `graph.html` do not rot.

## Other conventions

- Agent work goes in a dedicated git worktree; do not edit the user's main
  checkout directly unless explicitly asked.
- Runtime artifacts (`*.log`, `src/data/portfolio_state.json`, `server_5000.log`)
  are not code — do not commit them.
