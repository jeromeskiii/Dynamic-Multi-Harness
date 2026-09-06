# Dynamic Multi-Harness (DMH)

A Terraform-style host that owns authoritative session state and swaps replaceable harness runtimes behind a frozen ABI. The dynamic part is a bind policy over a frozen ABI, not a smarter loop.

> **One source of model-visible truth. Many replaceable drivers.**

---

## 1. Topologies & Dynamic Scheduling

| Topology | Verdict |
|---|---|
| A. Profile swap inside one kernel | A mode of B (day-one presets) |
| **B. Host + foreign harnesses as backends** | **The one that works — this build** |
| C. Peer mesh (A2A) | Later, for harnesses you do not spawn |
| D. In-process "import both loops" | Do not do this |

A host holds authoritative session state and speaks the ABI out to whatever runtime binary is current:

1. **Per-session bind**: Profile selects initial runtime (`native`, `child`, `ev1h-007`, or `cognihak`).
2. **Per-turn rebind**: `switch_runtime(to, reason)` executes strictly at turn boundaries (never mid-stream), appends `runtime/switch {from, to, reason, epoch}`, and requires `replay.from_log: true` on the incoming runtime.
3. **Per-step specialist**: `delegate_harness(runtime, objective)` opens a child session with `lineage = parent`, `delegation_depth += 1`, returning results as `tool/result` (recursion depth guarded at 2).
4. **Authoritative Event Log**: The host owns `derive_messages()`. Model history is derived dynamically from log events, never from a runtime's transient memory.

---

## 2. Division of Ownership

| What the Host Owns (Shared & Authoritative) | What Runtimes Own (Private & Ephemeral) |
|---|---|
| Session ID, epoch, lineage, delegation depth | Prompt assembly internals & templating |
| Append-only session log + `derive_messages()` | Private loop state machine & turn scheduling |
| Inbox (`user/message` facts) | Private tool registries & execution engines |
| Approval plane & credentials seam | Compaction strategies & token cache |
| Sandbox world (`fs.read` via reserved transport) | UI chrome & transport carriers |
| Provenance: which runtime produced which events | Model client weights & network sessions |

---

## 3. Quick Start & Phase Gates

Zero runtime dependencies for the DMH host kernel. Requires Python 3.9+.

```bash
# Run the 9 named phase verification gates (fail-loud)
python3 scripts/verify-phase-gates.py

# Run full unittest discovery suite (57 tests)
python3 -m unittest discover -s tests -t .

# Run dynamic switch demo (native -> foreign child -> delegate)
python3 examples/demo_dynamic.py

# Run EV1H-007 real foreign runtime demo (native -> switch -> EV1H-007)
python3 examples/demo_ev1h.py

# Run cognihak translated foreign runtime demo (cognihak -> switch out -> native)
python3 examples/demo_cognihak.py

# Run Qwen3.8-Flash-Next translated foreign runtime demo (qwen -> switch out -> native)
python3 examples/demo_qwen.py
```

### The 9 Phase Gates

| Phase | Gate File | Proves |
|---|---|---|
| **1 Kernel** | `tests/test_phase1_log.py` | Append-only log, replay to identical projection, fork without parent mutation, surface vs control events |
| **2 Handshake** | `tests/test_phase2_handshake.py` | Cookie exit 2 with no line, ABI exit 3, stdout pollution, loopback binds, digest pins, initialize echo, `NOT_INITIALIZED`, no reattach |
| **3 Bind & Switch** | `tests/test_phase3_bind_switch.py` | Fail-closed presets, turn-boundary-only switch with epoch events, namespaced step IDs, `replay.from_log` requirement |
| **4 Delegate & Approval** | `tests/test_phase4_delegate.py` | Child sessions with lineage + depth guard, host-owned approval plane, unknown-tool denial, reserved fs transport |
| **5 Replay** | `tests/test_phase5_replay.py` | Same log, two runtimes, identical surface projection up to the switch marker |
| **6 Flow & Cancel** | `tests/test_phase6_flow.py` | Bounded buffer with declared overflow policy (`drop_oldest` / `pause`), cancel-before-dispatch, mid-stream drain |
| **7 Foreign Runtime** | `tests/test_phase7_ev1h.py` | EV1H-007 bound as a foreign backend: interpreter/root resolution, bind caps, submit, turn-boundary switch, capability gate fails closed |
| **8 Translator Runtime** | `tests/test_phase8_cognihak.py` | cognihak driven through its own protocol: bind, streamed turn, consecutive turns, and fail-closed switch-into |
| **9 Qwen Runtime** | `tests/test_phase9_qwen.py` | Qwen3.8-Flash-Next driven through H1 JSON-RPC: bind, streamed turn, consecutive turns, and fail-closed switch-into |

---

## 4. Foreign Runtime Backends

### A. EV1H-007 (`dmh/ev1h_runtime.py`)
Binds the defensive evidence harness from [`/Users/ohmskiii/EV1H-007`](../EV1H-007).
- **Protocol**: Direct DMH ABI over stdio NDJSON (`harness.dmh_serve serve`).
- **Isolation**: Independent Python 3.11+ virtualenv with PyYAML; host environment never leaks to the child.
- **Path Resolution**: `EV1H_ROOT` env var or sibling directory `../EV1H-007`; `EV1H_PYTHON` or `<root>/.venv/bin/python3`.
- **Capabilities**: `streaming: true`, `replay.from_log: true` (supports turn-boundary `switch_runtime` **into** and **out of** EV1H-007).
- **Safety Gate**: `tools.native: false` ensures `coding` preset fails closed with `CAPABILITY_REQUIRED`.

```python
from dmh.ev1h_runtime import available, register
from dmh.host import Host

host = Host(store_dir=".dmh_state")
register(host)

session = host.create_session("default")
session.bind("ev1h-007")
session.submit("Inspect Suricata EVE logs for SID 9100001")
```

### B. cognihak (`dmh/cognihak_runtime.py`)
Binds the H1 security & cognitive dataset harness from [`/Users/ohmskiii/cognihak`](../cognihak).
- **Protocol**: Host-side translator driving cognihak's native stdio JSON-RPC carrier (`pnpm cog --profile protocol`).
- **Translation Mapping**: `initialize` (boot probe + session), `step/run` (`agent/send`), `step/chunk` (`assistant/chunk`), `shutdown`.
- **Path Resolution**: `COGNIHAK_ROOT` env var or sibling `../cognihak`; `COGNIHAK_TSX` or `<root>/node_modules/.bin/tsx`.
- **Capabilities**: `streaming: true`, `replay.from_log: false`.
- **Fail-Closed Boundary**: Because cognihak does not accept external history injection on `agent/send`, DMH enforces `replay.from_log: false`. A session can bind cognihak or switch *out* of it, but switching *into* cognihak mid-session is safely rejected with `CAPABILITY_REQUIRED`.

```python
from dmh.cognihak_runtime import available, register
from dmh.host import Host

host = Host(store_dir=".dmh_state")
register(host, api_base="http://127.0.0.1:8000/v1")

session = host.create_session("default")
session.bind("cognihak")
session.submit("Analyze cognitive attack vectors in sample 42")
```

### C. Qwen3.8-Flash-Next (`dmh/qwen_runtime.py`)
Binds the H1 agent harness from [`/Users/ohmskiii/Qwen3.8-Flash-Next-Harness`](../Qwen3.8-Flash-Next-Harness).
- **Protocol**: Host-side translator driving Qwen's stdio JSON-RPC protocol carrier (`pnpm dsh --profile protocol`).
- **Path Resolution**: `QWEN_ROOT` env var or sibling `../Qwen3.8-Flash-Next-Harness`; `QWEN_TSX` or `<root>/node_modules/.bin/tsx`.
- **Capabilities**: `streaming: true`, `replay.from_log: false`.
- **Fail-Closed Boundary**: Supports initial bind and switch *out* to native/child runtimes; mid-session switch *into* Qwen safely fails closed with `CAPABILITY_REQUIRED`.

```python
from dmh.qwen_runtime import available, register
from dmh.host import Host

host = Host(store_dir=".dmh_state")
register(host, api_base="http://127.0.0.1:8000/v1")

session = host.create_session("default")
session.bind("qwen3.8-flash-next")
session.submit("Explain hybrid attention architecture")
```

---

## 5. ABI Handshake Protocol (Layer 0 & Layer 1)

### Layer 0 — Process Identity & Security Boundaries
- **Magic Cookie**: `HARNESS_PLUGIN_MAGIC_COOKIE` — missing or invalid value causes exit code 2 and human stderr message with no handshake line.
- **Protocol Versions**: `HARNESS_PLUGIN_PROTOCOL_VERSIONS` — preference-ordered ABI versions; mismatch exits 3.
- **Network Safety**: Unix sockets or loopback TCP only (`127.0.0.1` / `::1`). Binding to `0.0.0.0` triggers immediate kill and `security/finding`.
- **Environment Allowlist**: Child processes receive a scrubbed environment (`PATH`, `HOME`, `TMPDIR`, explicit `COG_*` / `EV1H_*` keys). High-privilege tokens (`AWS_*`, `OPENAI_*`, `GH_TOKEN`, `SSH_AUTH_SOCK`) are stripped.

### Layer 1 — Initialize & Capability Algebra
NDJSON JSON-RPC 2.0. `initialize` must be the first method:
- **Capability Rule**: Omitted = unsupported = `false` (never default-true).
- **Effective Calculation**: `effective(c) = hostCapabilities[c] AND runtimeCapabilities[c]` for tracked resources (`fs.world`, `sandbox.world`, `approval.ask`).
- **Preset Enforcement**: Presets (`default`, `coding`, `research`) define required capabilities; any unmet requirement fails closed with `CAPABILITY_REQUIRED`.

### Session Event Surface
- **Model-Visible Surface Events**:
  ```text
  runtime/bind  runtime/switch  turn/start  step/start  user/message
  assistant/chunk*  assistant/message  tool/call  tool/result
  step/end  turn/end  runtime/unbind
  ```
- **Control & Audit Events (Log-Only)**:
  ```text
  runtime/launch  runtime/handshake  runtime/initialize  runtime/bind-failed
  approval/request  approval/decided  session/delegate  turn/abort
  turn/abort-before-dispatch  security/finding  overflow/dropped  overflow/paused
  ```

---

## 6. Programmatic Python API

```python
from dmh.host import Host
from dmh.native import ScriptedNative
from dmh.child_runtime import ChildRuntime
from dmh.ev1h_runtime import register as register_ev1h
from dmh.cognihak_runtime import register as register_cognihak
from dmh.qwen_runtime import register as register_qwen

# 1. Initialize Host with persistent state directory
host = Host(store_dir=".dmh_state")

# 2. Register native and foreign providers
host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
host.register("child", lambda: ChildRuntime(sandbox=host.sandbox))
register_ev1h(host)
register_cognihak(host)
register_qwen(host)

# 3. Create session and execute turns
session = host.create_session("session-01")
session.bind("native")
session.submit("Initial reconnaissance")

# 4. Rebind across turn boundaries
session.switch_runtime("ev1h-007", reason="forensics_specialist")
session.submit("Deep packet inspection on Suricata EVE logs")

# 5. Extract authoritative model-visible conversation history
for msg in session.log.derive_messages():
    print(f"[{msg['role']}] {msg['content']}")
```

### Multi-runtime switching within one session

A session can rebind across as many runtimes as the profile permits, as long
as every `switch_runtime(to, ...)` happens at a turn boundary and the
*target* runtime advertises `replay.from_log: true`. A round-trip
(`A → B → A`) is legal: each switch increments the `epoch`, appends a
`runtime/unbind {runtime, reason}` for the previous binding, a
`runtime/switch {from, to, reason, epoch}` record, and the new bind trail.

```python
session = host.create_session("default")
session.bind("native")
session.submit("Map the redirector chain")

# Mid-session specialist swap
session.switch_runtime("child", reason="specialist")
session.submit("Continue with the foreign runtime")

# Rebind back to the home runtime at the next turn boundary
session.switch_runtime("native", reason="back on home runtime")
session.submit("Wrap up on the local runtime")

# Session log: bind(1), submit(1), unbind(1), switch(2),
# bind(2), submit(2), unbind(2), switch(3), bind(3), submit(3).
# derive_messages() returns three user/assistant pairs in order,
# each produced by the runtime bound when that turn started.
```

Every `step_id` carries the `runtime:` prefix that produced it; every
`step/start` logs the bound runtime's `schema_hash`, so a downstream
reader can verify which runtime wrote which fact without trusting the
host's RAM. The full A → B → A loop is exercised end-to-end by
`tests/test_multi_runtime_switch.py`.

> **Note on step-id namespacing.** `step_no` resets at the start of every
> turn, so the *same* runtime rebound later in a session can emit a
> `step_id` it already used. The namespacing claim is honored across
> **distinct** runtimes (a `native:...` id never collides with a
> `child:...` id) but not across rebinds of one runtime. If you need
> strict per-session uniqueness, record the turn number in the id.

---

## 7. Repository Layout

```text
dmh/
├── host.py              Host kernel: sessions, bind/switch, turns, delegation, approvals
├── events.py            Append-only session log + derive_messages + deterministic fork
├── handshake.py         Layer 0: cookie validation, env allowlist, handshake line, digest
├── wire.py              NDJSON JSON-RPC framing, deadline LineReader/LineWriter
├── provider.py          HarnessProvider protocol (the host-side ABI contract)
├── child_runtime.py     Host-side adapter: spawn + speak ABI to child binary
├── child.py             Reference foreign runtime binary (`python -m dmh.child serve`)
├── ev1h_runtime.py      Host-side adapter: spawn EV1H-007 foreign runtime
├── cognihak_runtime.py  Host-side translator: drive cognihak's JSON-RPC protocol
├── qwen_runtime.py      Host-side translator: drive Qwen's JSON-RPC protocol
├── native.py            In-process native loop driver (day-one default provider)
├── capabilities.py      Capability algebra & fail-closed resolution
├── presets.py           Default / coding / research required-capability sets
├── flow.py              Bounded event buffer with overflow policies (drop_oldest / pause)
└── errors.py            Standardized DMH error codes & exception hierarchy

examples/
├── demo_dynamic.py      Native -> foreign child -> delegation workflow
├── demo_ev1h.py         Native -> switch -> EV1H-007 foreign runtime workflow
├── demo_cognihak.py     cognihak -> switch out -> native workflow
└── demo_qwen.py         qwen -> switch out -> native workflow

scripts/
└── verify-phase-gates.py Fail-loud runner for the 9 architectural phase gates

tests/
├── test_phase1_log.py          Phase 1: Event log & replay invariants
├── test_phase2_handshake.py    Phase 2: Layer-0 / Layer-1 handshake & security
├── test_phase3_bind_switch.py  Phase 3: Bind, switch & preset algebra
├── test_phase4_delegate.py     Phase 4: Delegation, approvals & sandbox
├── test_phase5_replay.py       Phase 5: Cross-runtime session replay
├── test_phase6_flow.py         Phase 6: Flow control, buffers & cancellation
├── test_phase7_ev1h.py         Phase 7: EV1H-007 foreign backend gate
├── test_phase8_cognihak.py     Phase 8: cognihak translator backend gate
├── test_phase9_qwen.py         Phase 9: Qwen3.8-Flash-Next translator backend gate
├── test_multi_runtime_switch.py Multi-runtime A↔B rebind within one session
└── stub_openai.py              Offline deterministic OpenAI-compatible stub
```

---

## 8. License

Apache 2.0.
