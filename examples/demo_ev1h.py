"""EV1H-007 as a foreign runtime: native turn -> switch -> foreign turn.

The point of the exercise: the host never learns anything about EV1H-007's
internals. It binds a runtime id, speaks the frozen ABI, and derives the next
request from its own log. The switch is legal only at a turn boundary, and the
incoming runtime rebuilds its context from the host projection rather than
from anything it remembered.

Requires the EV1H-007 checkout next to this one (or EV1H_ROOT in the
environment). It exits with a message rather than a traceback when absent.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from dmh.ev1h_runtime import available, register
from dmh.host import Host
from dmh.native import ScriptedNative


def main():
    store = tempfile.mkdtemp(prefix="dmh-ev1h-")
    host = Host(store_dir=store)
    host.sandbox.write("notes.txt", "sandboxed note for the demo")

    host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
    register(host)

    if not available():
        print("EV1H-007 foreign runtime not available; nothing to show.")
        print("Set EV1H_ROOT (and EV1H_PYTHON) or clone EV1H-007 next to this repo.")
        return 1

    session = host.create_session("default")
    session.bind("native")
    session.submit("Map the redirector chain")
    print("turn 1 (native):       ", session.log.last_assistant_text())

    session.switch_runtime("ev1h-007", reason="specialist")

    session.submit("Continue with the EV1H-007 runtime")
    print("turn 2 (ev1h-007):     ", session.log.last_assistant_text())

    print()
    print("session log (surface facts, model-visible):")
    for msg in session.log.derive_messages():
        print(f"  [{msg['role']}] {msg['content'][:70]}")

    switches = [e for e in session.log.events if e.kind == "runtime/switch"]
    print()
    print(f"runtime/switch events: {len(switches)}")
    print(f"events on disk: {len(session.log.reload())} (store: {store})")

    host.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
