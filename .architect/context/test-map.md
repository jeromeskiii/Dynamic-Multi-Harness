# Test Map

- Framework: stdlib `unittest`; tests are under `tests/` and can be discovered with `python3 -m unittest discover -s tests -t .`.
- Phase 1: `test_phase1_log.py` — append-only persistence, replay, projection, fork, event surfaces.
- Phase 2: `test_phase2_handshake.py` — cookie/ABI negotiation, framing, environment security, loopback, initialization lifecycle.
- Phase 3: `test_phase3_bind_switch.py` — presets, bind/switch boundaries, epochs, namespaced steps, replay requirements.
- Phase 4: `test_phase4_delegate.py` — delegation lineage/depth, approvals, tool denial, reserved filesystem transport.
- Phase 5: `test_phase5_replay.py` — identical projections across runtimes and disk replay.
- Phase 6: `test_phase6_flow.py` — bounded buffers, overflow, cancellation, turn limits, drain behavior.
- Phase 7: `test_phase7_ev1h.py` — EV1H-007 backend; skips if sibling runtime/toolchain is absent.
- Phase 8: `test_phase8_cognihak.py` — cognihak translator; skips if sibling runtime/toolchain is absent.
- Phase 9: `test_phase9_qwen.py` — Qwen translator; skips if sibling runtime/toolchain is absent.
- `test_multi_runtime_switch.py` — native/child/native round-trip and event ordering.
- Focused gate command: `python3 scripts/verify-phase-gates.py`.
- No separate lint/typecheck configuration was found in the inspected top level.

