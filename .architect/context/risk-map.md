# Risk Map

- `dmh/host.py` and `dmh/events.py` are high-impact: changes can break authoritative history, replay determinism, event ordering, or persistence.
- `dmh/handshake.py`, `dmh/wire.py`, `dmh/child_runtime.py`, and `dmh/child.py` are security/protocol-sensitive: preserve fail-closed cookie, ABI, digest, stdout, environment, and loopback checks.
- `dmh/capabilities.py`, `presets.py`, and foreign adapters gate permissions and runtime switching; omitted capabilities must remain unsupported.
- Foreign adapters depend on external sibling repos and Node/Python toolchains; validate both skip behavior and available-runtime behavior.
- `.dmh_state/`, caches, bytecode, logs, secrets, and generated artifacts should not be edited or committed.
- Required validation before runtime changes: phase gates plus full unittest discovery; run the relevant demo when external dependencies are involved.

