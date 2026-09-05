"""Day-one demo of the dynamic multi-harness (design doc, minimal build).

Shows: host-owned session log -> native runtime -> turn-boundary switch to a
spawned foreign runtime (ACP-style NDJSON over stdio) -> delegate_harness
child session -> replay projection.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dmh.child_runtime import ChildRuntime
from dmh.host import Host
from dmh.native import ScriptedNative


def main():
    store = tempfile.mkdtemp(prefix="dmh-demo-")
    host = Host(store_dir=store)
    host.sandbox.write("notes.txt", "sandboxed note for the demo")

    host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
    host.register("child", lambda: ChildRuntime(sandbox=host.sandbox))

    session = host.create_session("default")
    session.bind("native")

    session.submit("Design a safe agent execution loop")
    print("turn 1 (native):", session.log.last_assistant_text())

    session.switch_runtime("child", reason="specialist")
    session.submit("Continue, but use the foreign runtime")
    print("turn 2 (child): ", session.log.last_assistant_text())

    child = session.delegate_harness("native", "Read the sandbox note and echo it")
    print("delegated:", child.log.last_assistant_text() if child else "refused")

    print()
    print("session log (surface facts, model-visible):")
    for msg in session.log.derive_messages():
        print(f"  [{msg['role']}] {msg['content'][:70]}")
    print()
    print(f"events on disk: {len(session.log.reload())} (store: {store})")

    host.stop_all()


if __name__ == "__main__":
    main()
