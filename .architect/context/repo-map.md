# Repository Map

- Project: Dynamic Multi-Harness (DMH), a Python host for authoritative session logs and replaceable harness runtimes behind a frozen ABI.
- Language/tooling: Python 3.9+, setuptools package, stdlib `unittest`; no runtime dependencies.
- `dmh/`: host kernel, event log, ABI handshake/wire protocol, provider contract, native/child adapters, foreign-runtime translators, capability/preset/flow/error modules.
- `tests/`: nine phase gates plus multi-runtime switching coverage and an offline OpenAI-compatible stub.
- `examples/`: dynamic child/delegation demo and EV1H-007, cognihak, and Qwen translator demos.
- `scripts/verify-phase-gates.py`: fail-loud runner for the nine named phase test files.
- Public entrypoints: `dmh.host.Host`, provider registration functions, `python -m dmh.child serve`, and the example scripts.
- Validation from README: `python3 scripts/verify-phase-gates.py`; `python3 -m unittest discover -s tests -t .`.
- Ignored/generated areas: `__pycache__/`, `.pytest_cache/`, `.dmh_state/`, `.DS_Store`.

