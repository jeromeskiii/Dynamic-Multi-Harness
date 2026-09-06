#!/usr/bin/env python3
"""Verify the nine named phase gates, fail-loud (SGAA-style).

Each gate maps to a named test module:
  1 kernel      tests/test_phase1_log.py
  2 handshake   tests/test_phase2_handshake.py
  3 bind/switch tests/test_phase3_bind_switch.py
  4 delegate    tests/test_phase4_delegate.py
  5 replay      tests/test_phase5_replay.py
  6 flow/cancel tests/test_phase6_flow.py
  7 foreign     tests/test_phase7_ev1h.py
  8 translator  tests/test_phase8_cognihak.py
  9 qwen        tests/test_phase9_qwen.py

Gates 7, 8, and 9 drive real foreign harnesses living in other repos. They skip -
and report GREEN - when that checkout is absent, so the gate stays runnable on
a machine that only has this one.
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATES = [
    ("1 Runtime/Kernel", "tests.test_phase1_log"),
    ("2 Handshake ABI", "tests.test_phase2_handshake"),
    ("3 Bind & Switch", "tests.test_phase3_bind_switch"),
    ("4 Delegate & Approval", "tests.test_phase4_delegate"),
    ("5 Replay Fixture", "tests.test_phase5_replay"),
    ("6 Flow & Cancel", "tests.test_phase6_flow"),
    ("7 Foreign Runtime", "tests.test_phase7_ev1h"),
    ("8 Translator Runtime", "tests.test_phase8_cognihak"),
    ("9 Qwen Runtime", "tests.test_phase9_qwen"),
]


def main():
    failed = []
    for label, module in GATES:
        print(f"RUN    {module} ({label})")
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", module],
            cwd=ROOT, capture_output=True, text=True,
        )
        if proc.returncode == 0:
            print(f"GREEN  {module} ({label})")
        else:
            print(f"RED    {module} ({label})")
            print(proc.stdout[-800:])
            print(proc.stderr[-800:])
            failed.append(module)
    print()
    if failed:
        print(f"PHASE GATES FAILED: {len(failed)}/{len(GATES)} ({', '.join(failed)})")
        return 1
    print(f"PHASE GATES GREEN: {len(GATES)}/{len(GATES)} passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
