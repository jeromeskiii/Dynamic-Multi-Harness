"""Qwen3.8-Flash-Next-Harness as a foreign runtime: bind -> stream -> switch out.

Exercises the real Qwen harness (dsh --profile protocol) through the DMH
translator against an offline stub OpenAI-compatible endpoint.

Requires Qwen3.8-Flash-Next-Harness checkout next to this repo (or QWEN_ROOT).
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dmh import qwen_runtime
from dmh.errors import HarnessError
from dmh.host import Host
from dmh.native import ScriptedNative
from dmh.qwen_runtime import QwenRuntime, available, register
from tests.stub_openai import StubOpenAI


def main():
    if not available():
        print("Qwen3.8-Flash-Next-Harness not available; skipping demo.")
        print("Clone Qwen3.8-Flash-Next-Harness next to this repo or set QWEN_ROOT.")
        return 1

    stub = StubOpenAI().start()
    store = tempfile.mkdtemp(prefix="dmh-qwen-demo-")
    host = Host(store_dir=store)
    host.sandbox.write("qwen_notes.txt", "Qwen harness demo workspace")

    host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
    register(host, api_base=stub.base_url)

    session = host.create_session("default")
    session.bind("qwen3.8-flash-next")

    session.submit("Explain hybrid attention architecture")
    print("turn 1 (qwen):", session.log.last_assistant_text())

    session.submit("What is the activated parameter count?")
    print("turn 2 (qwen):", session.log.last_assistant_text())

    print()
    print("session log (surface facts, model-visible):")
    for msg in session.log.derive_messages():
        print(f"  [{msg['role']}] {msg['content'][:70]}")

    print()
    try:
        session.switch_runtime("native", reason="switch_to_native")
        print("switch OUT of qwen: ok (native requires only streaming)")
    except HarnessError as exc:
        print(f"switch OUT failed: {exc}")

    session.submit("Back on the native runtime")
    print("turn 3 (native):  ", session.log.last_assistant_text())

    try:
        session.switch_runtime("qwen3.8-flash-next", reason="switch_back")
        print("switch INTO qwen: unexpected success")
    except HarnessError as exc:
        print(f"switch INTO qwen: refused ({exc.code}) - replay.from_log is false")

    host.stop_all()
    stub.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
