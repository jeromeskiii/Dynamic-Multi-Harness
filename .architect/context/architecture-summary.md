# Architecture Summary

- The host owns session identity, append-only event logs, derived model history, inbox, approvals, sandbox access, and runtime provenance.
- Runtimes implement the `HarnessProvider` contract and retain only private loop/transport state. Native and child runtimes support replay from the host log; cognihak and Qwen are fail-closed for mid-session switch-in because their carriers do not accept injected history.
- A session binds a runtime at an epoch. Switching is allowed only at turn boundaries, records unbind/switch/bind audit events, and requires target `replay.from_log`.
- Turns project the authoritative log into messages, stream steps/chunks/tool events through the host, and close with `turn/end` or an abort event. Delegation creates child sessions with lineage and a depth guard.
- Child processes use NDJSON JSON-RPC with cookie/version handshake, scrubbed environment, digest pinning, and loopback-only transport. Reserved host services expose sandbox reads.
- External integrations are optional sibling projects: EV1H-007, cognihak, and Qwen harnesses. Their tests skip when unavailable.
- Inferred open question: README describes sibling paths and optional toolchains, but this checkout does not itself contain those projects; availability must be confirmed by tests/runtime probes.

