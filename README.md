# Dynamic Multi-Harness (DMH)

A Terraform-style host that owns authoritative session state and swaps
replaceable harness runtimes behind a frozen ABI. The dynamic part is a bind
policy over a frozen ABI, not a smarter loop.

> **One source of model-visible truth. Many replaceable drivers.**

## What "dynamic" means here

| Topology | Verdict |
|---|---|
| A. Profile swap inside one kernel | A mode of B (day-one presets) |
| **B. Host + foreign harnesses as backends** | **The one that works - this build** |
| C. Peer mesh (A2A) | Later, for harnesses you do not spawn |
| D. In-process "import both loops" | Do not do this |

A host holds authoritative session state and speaks the ABI out to whatever
runtime binary is current. That is the dynamic harness.

## What the host owns (shared)

- session id, epoch, lineage, delegation depth
- append-only session log + `derive_messages()` (model-visible truth)
- inbox (`user/message` facts), approval plane, credentials seam
- sandbox world (`fs.read` via the reserved transport)
- cancellation and turn/step lifecycle as facts
- provenance: which runtime produced which events

## What runtimes own (never shared)

- prompt assembly internals, private loop state machine
- tool registry, compaction strategy, UI chrome

## The rule that makes it possible

If two harnesses both append `assistant/message` and `tool/result` with the
same surface schema, the host can swap drivers mid-session. Reconstruction
epochs stay host-side: after a switch, the next request is derived from the
log, never from a runtime's leftover RAM.

## Quick start

Zero runtime dependencies. Python 3.9+.

```bash
# the six named gates, fail-loud
python3 scripts/verify-phase-gates.py

# everything (unittest discover)
python3 -m unittest discover -s tests -t .

# end-to-end: native turn -> switch -> foreign runtime -> delegate
python3 examples/demo_dynamic.py

# end-to-end with a real foreign harness (needs the EV1H-007 checkout)
python3 examples/demo_ev1h.py

# end-to-end with a translated foreign harness (needs the cognihak checkout)
python3 examples/demo_cognihak.py
```

## Phase gates

| Phase | Gate | Proves |
|---|---|---|
| 1 Kernel | `tests/test_phase1_log.py` | append-only log, replay -> identical projection, fork without parent mutation, surface vs control events |
| 2 Handshake | `tests/test_phase2_handshake.py` | cookie exit 2 with no line, ABI exit 3, stdout pollution, loopback binds, digest pins, initialize echo, NOT_INITIALIZED, no reattach |
| 3 Bind & Switch | `tests/test_phase3_bind_switch.py` | fail-closed presets, turn-boundary-only switch with epoch events, namespaced step ids, replay.from_log requirement |
| 4 Delegate & Approval | `tests/test_phase4_delegate.py` | child sessions with lineage + depth guard, host-owned approval plane, unknown-tool denial, reserved fs transport |
| 5 Replay | `tests/test_phase5_replay.py` | same log, two runtimes, identical surface projection up to the switch marker |
| 6 Flow & Cancel | `tests/test_phase6_flow.py` | bounded buffer with declared overflow policy, cancel-before-dispatch, mid-stream drain |
| 7 Foreign runtime | `tests/test_phase7_ev1h.py` | EV1H-007 bound as a foreign backend: interpreter/root resolution, bind caps, submit, turn-boundary switch, capability gate fails closed |
| 8 Translator runtime | `tests/test_phase8_cognihak.py` | cognihak driven through its own protocol: bind, streamed turn, consecutive turns, and the fail-closed switch-into |


## ABI handshake (Layer 0 + Layer 1)

### Layer 0 - process identity

- `HARNESS_PLUGIN_MAGIC_COOKIE` - wrong value = human stderr message, exit 2,
  **no handshake line** (cookie is UX, not auth)
- `HARNESS_PLUGIN_PROTOCOL_VERSIONS` - preference-ordered ABI majors; no
  overlap = stderr diagnosis, exit 3
- Handshake line (unix/tcp only; stdio skips it):

```
1|2|unix|/tmp/h-8f2a.sock|jsonrpc||0|<digest>
CORE|ABI|NET|ADDR|WIRE|CERT|MUX|DIGEST
```

- tcp binds must be loopback-only; `0.0.0.0` = kill + `security/finding`
- digest pins are verified against the hashed file on disk (`DIGEST_MISMATCH`)
- child env is allowlist-only: no `AWS_*`, `OPENAI_*`, `GH_TOKEN`,
  `SSH_AUTH_SOCK`, ever

### Layer 1 - initialize

NDJSON JSON-RPC 2.0. `initialize` is the first and only legal first method:

- `abiVersion` echoes the Layer-0 line (socket transports)
- `runtimeCapabilities`: **omitted = unsupported = false**, never default-true
- `effective(c) = hostCapabilities[c] AND runtimeCapabilities[c]` for keys the
  host tracks (`fs.world`, `sandbox.world`, `approval.ask`); runtime-only
  capabilities (`streaming`, `tools.native`, `compaction`) are the runtime's
  own value
- presets are required-capability sets: a missing capability fails the bind
  closed with `CAPABILITY_REQUIRED`

### Session surface (the join key)

```
runtime/bind  runtime/switch  turn/start  step/start  user/message
assistant/chunk*  assistant/message  tool/call  tool/result
step/end  turn/end  runtime/unbind
```

Log-only (never enter `derive_messages()`): `runtime/launch`,
`runtime/handshake`, `runtime/initialize`, `runtime/bind-failed`,
`approval/request`, `approval/decided`, `session/delegate`,


## How dynamism is scheduled

1. **Per-session bind** - profile picks `runtime = native` or `runtime = child`
2. **Per-turn rebind** - `switch_runtime(to, reason)`, legal only at turn
   boundaries (never mid-stream), appends `runtime/switch {from,to,reason,epoch}`
   and requires `replay.from_log` on the incoming runtime
3. **Per-step specialist** - `delegate_harness(runtime, objective)` opens a
   child session with `lineage = parent`, `delegation_depth += 1`, results
   return as `tool/result`; depth guard at 2
4. Parallel contest - fork must refuse an open-turn boundary (Phase 1 gate)

## Python API

```python
from dmh import Host
from dmh.native import ScriptedNative
from dmh.child_runtime import ChildRuntime

host = Host(store_dir=".dmh_state")
host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
host.register("child", lambda: ChildRuntime(sandbox=host.sandbox))

session = host.create_session("default")
session.bind("native")
session.submit("Design a safe agent execution loop")
session.switch_runtime("child", reason="specialist")
session.submit("Continue with the foreign runtime")
session.delegate_harness("native", "Read the sandbox note")
print(session.log.derive_messages())
```

`turn/abort`, `turn/abort-before-dispatch`, `security/finding`,
`overflow/dropped`, `overflow/paused`.


## A real foreign runtime: EV1H-007

`dmh/ev1h_runtime.py` binds **EV1H-007** — a separate project, different
interpreter, different dependency set — as a backend. It is
the proof that topology B is not a diagram: the host learns nothing about how
EV1H-007 assembles prompts or runs its loop.

```python
from dmh.ev1h_runtime import register
from dmh.host import Host

host = Host(store_dir=".dmh_state")
register(host)
session = host.create_session("default")
session.bind("ev1h-007")
```

The spawn is resolved, never assumed:

| Setting | Resolution order |
|---|---|
| interpreter | `python=` arg → `EV1H_PYTHON` → `<root>/.venv/bin/python3` → `sys.executable` |
| project root | `project_root=` arg → `EV1H_ROOT` → sibling `EV1H-007/` |

This matters because the two projects do not share a runtime: DMH is 3.9+ with
zero dependencies, EV1H-007 is 3.11+ with PyYAML. Spawning with
`sys.executable` would import EV1H-007's `harness` package under the wrong
interpreter. A missing interpreter or a root without a `harness/` package
fails loud at launch with `RUNTIME_FAULT` — it never silently falls back to
the DMH root.

The runtime side lives in EV1H-007 as `harness/dmh_serve.py`. It is stdlib-only
and **mirrors** the Layer-0 constants rather than importing them: a runtime
that imported the host could not diverge from it, so the digest pin would
prove nothing.

What that runtime does and does not claim:

| | |
|---|---|
| Drives | EV1H-007's real `AgentLoop.run_turn()` |
| Session truth | host-side; rebuilt from the projection on every `turn/open` |
| Streaming | event-level, mirrored on append — not token-level |
| Tools | none registered → `tools.native` false |
| Cancellation | step-boundary only → **`cancel.fuse` not advertised** |

Two consequences worth internalising. `tools.native: false` means the `coding`
preset fails closed with `CAPABILITY_REQUIRED` instead of half-running — that
is the gate working, not a limitation to work around. And because
`run_turn()` has no interrupt point, cancellation is observable only between
steps; claiming `cancel.fuse` would make the host's phase-6 guarantee a lie.

## A translated foreign runtime: cognihak

`dmh/cognihak_runtime.py` binds **cognihak** — a TypeScript harness that
already speaks newline-delimited JSON-RPC over stdio, but with its own method
set and its own async model. This provider does not speak the DMH ABI to the
child; it *translates*:

| DMH ABI | cognihak protocol |
|---|---|
| launch | spawn `cog --profile protocol` |
| initialize | `session/list` (boot probe) + `session/create` |
| step/run | `agent/send`, then poll `session/events` |
| step/chunk | `assistant/chunk` delta |
| turn complete | `turn/end` event |
| cancel | step-boundary flag only — no `agent/cancel` on their carrier |
| shutdown | `shutdown` + close stdin |

cognihak needs an OpenAI-compatible model endpoint. The tests and the demo
ship a stub (`tests/stub_openai.py`), so the genuine adapter path — fetch,
SSE framing, delta handling — is exercised offline and deterministically.

```python
from dmh.cognihak_runtime import register
from dmh.host import Host

host = Host(store_dir=".dmh_state")
register(host, api_base="http://127.0.0.1:8000/v1")
session = host.create_session("default")
session.bind("cognihak")
```

### The boundary: bind-only, and why

The DMH ABI assumes a runtime can rebuild itself from the host's log
projection. cognihak's protocol has no way to inject history — `agent/send`
takes only a message — so this runtime declares **`replay.from_log: false`**.
The host takes the claim seriously: `switch_runtime` requires
`replay.from_log`, so a session can bind cognihak but **cannot switch to it
mid-session**. The switch fails closed with `CAPABILITY_REQUIRED` instead of
silently handing the foreign runtime a conversation it cannot see.

The asymmetry is real and demonstrable (`examples/demo_cognihak.py`): switching
*out* of cognihak works, switching *into* it is refused. True mid-session
swap would require a shim inside cognihak that can seed its session log from
the host projection — deliberately not done, to keep that repo untouched.

## Deliberate scope cuts (per the design doc's own recommendations)

- **gRPC wire / broker mux deferred** - `MUX` is reserved, default `0`; the
  doc itself says "ship stdio JSON-RPC for ACP-compatible foreign harnesses"
  and only turn broker mux on when you need Terraform-style callbacks. NDJSON
  here is mux-by-message: the child calls back into host services
  (`host/fs_read`) on the same stream.
- **mTLS deferred** - the CERT field is parsed and validated; reattach is
  forbidden regardless (one spawn per bind, single connection per socket).
- **A2A mesh and parallel contest** - out of scope for the minimal dynamic
  build; A2A is for harnesses you do not spawn.
- **HTTP/2 flow control** - replaced by an app-level `BoundedEventBuffer`
  with declared overflow policy (`drop_oldest` / `pause`), which the doc
  requires for NDJSON-stdio runtimes anyway.

## Layout

```
dmh/
  host.py            host kernel: sessions, bind/switch, turns, delegation, approvals
  events.py          append-only session log + derive_messages + fork
  handshake.py       Layer 0: cookie, env allowlist, handshake line, digest
  wire.py            NDJSON JSON-RPC framing, deadline LineReader/LineWriter
  provider.py        HarnessProvider protocol (the ABI contract, host side)
  child_runtime.py   host-side adapter: spawn + speak the ABI to a child
  child.py           the foreign runtime binary: python -m dmh.child serve
  ev1h_runtime.py    host-side adapter: spawn the EV1H-007 foreign runtime
  cognihak_runtime.py host-side translator: drive cognihak's own protocol
  native.py          in-process native loop driver (day-one default provider)
  capabilities.py    capability algebra, fail-closed
  presets.py         default / coding / research required-capability sets
  flow.py            bounded event buffer with overflow policies
examples/demo_dynamic.py
examples/demo_ev1h.py
examples/demo_cognihak.py
scripts/verify-phase-gates.py
tests/test_phase{1..6}_*.py
```

## License

Apache 2.0.


