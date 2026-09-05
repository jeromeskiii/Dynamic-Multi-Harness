"""cognihak as a foreign runtime: bind -> streamed turn -> refused switch.

Demonstrates the translator provider: DMH speaks its own ABI, cognihak speaks
its own protocol, and the provider translates between them. It also shows the
honest limit: because cognihak's protocol cannot reconstruct host history,
switch_runtime INTO cognihak is refused (CAPABILITY_REQUIRED) - bind-only by
design.

Requires the cognihak checkout next to this one (or COGNIHAK_ROOT) with node
and tsx available. The model endpoint is a local stub, so nothing here needs
a real model or network access.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"
))

from dmh import cognihak_runtime
from dmh.cognihak_runtime import CognihakRuntime, register
from dmh.errors import HarnessError
from dmh.host import Host
from dmh.native import ScriptedNative
from stub_openai import StubOpenAI


def main():
    if not cognihak_runtime.available():
        print("cognihak runtime not available; nothing to show.")
        print("Set COGNIHAK_ROOT (and COGNIHAK_TSX) or clone cognihak next to this repo.")
        return 1

    store = tempfile.mkdtemp(prefix="dmh-cog-")
    host = Host(store_dir=store)
    host.sandbox.write("notes.txt", "sandboxed note for the demo")
    host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
    register(host)

    with StubOpenAI() as stub:
        def factory():
            return CognihakRuntime(
                api_base=stub.base_url,
                persistence_root=tempfile.mkdtemp(prefix="dmh-cog-run-"),
            )
        host.runtimes["cognihak"].factory = factory

        session = host.create_session("default")
        session.bind("cognihak")
        session.submit("Map the redirector chain")
        print("turn 1 (cognihak):", session.log.last_assistant_text())
        session.submit("And the cover server?")
        print("turn 2 (cognihak):", session.log.last_assistant_text())

    print()
    print("session log (surface facts, model-visible):")
    for msg in session.log.derive_messages():
        print(f"  [{msg['role']}] {msg['content'][:70]}")

    print()
    try:
        session.switch_runtime("native", reason="back to native")
        print("switch OUT of cognihak: ok (native requires only streaming)")
        session.submit("Back on the native runtime")
        print("turn 3 (native):  ", session.log.last_assistant_text())
    except HarnessError as exc:
        print(f"switch refused ({exc.code}): {exc.message}")
    try:
        session.switch_runtime("cognihak", reason="switch back")
    except HarnessError as exc:
        print(f"switch INTO cognihak: refused ({exc.code}) - replay.from_log is false")

    host.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
