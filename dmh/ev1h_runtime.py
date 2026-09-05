"""Host-side adapter: spawn the EV1H-007 foreign runtime.

Subclasses ChildRuntime to override spawn/digest specifics for the
EV1H-007 harness. The foreign shim speaks the same ABI as dmh/child.py.
"""

import os
import sys
from pathlib import Path

from .child_runtime import ChildRuntime
from .errors import HarnessError

EV1H_CAPABILITIES = {
    "streaming": True,
    "replay.from_log": True,
    "tools.native": False,
    "tools.code": False,
    "compaction": False,
    "approval.ask": False,
    "subagents.native": False,
    "plan": False,
    "goals": False,
    "mcp.client": False,
    "prompt.image": False,
    "fs.world": False,
    "sandbox.world": False,
    "runtime.multiSession": False,
}


EV1H_MODULE = "harness.dmh_serve"


def resolve_project_root(project_root=None):
    """Interpreter and root are resolved, never assumed: the two projects do
    not share a runtime (DMH is 3.9+/zero-dep, EV1H-007 is 3.11+/PyYAML), so
    guessing sys.executable would import `harness` under the wrong one."""
    if project_root is not None:
        return str(Path(project_root).resolve())
    env_root = os.environ.get("EV1H_ROOT")
    if env_root:
        return str(Path(env_root).resolve())
    default = Path(__file__).resolve().parents[1] / ".." / "EV1H-007"
    return str(default.resolve())


def resolve_python(python=None, project_root=None):
    if python is not None:
        return python
    env_python = os.environ.get("EV1H_PYTHON")
    if env_python:
        return env_python
    root = project_root or resolve_project_root()
    venv_python = os.path.join(root, ".venv", "bin", "python3")
    if os.path.isfile(venv_python) and os.access(venv_python, os.X_OK):
        return venv_python
    return sys.executable


def available(python=None, project_root=None):
    """Whether a bind can even be attempted. Callers gate on this rather than
    re-deriving the paths, so one resolution rule decides both."""
    root = project_root or resolve_project_root(project_root)
    interpreter = python or resolve_python(python, root)
    return (
        os.path.isfile(interpreter)
        and os.access(interpreter, os.X_OK)
        and os.path.isfile(os.path.join(root, "harness", "dmh_serve.py"))
    )


class EV1HRuntime(ChildRuntime):
    """A spawned EV1H-007 runtime child; the foreign harness as a provider."""

    runtime_id = "ev1h-007"

    def __init__(self, *, sandbox=None, python=None, project_root=None,
                 transport="stdio", offered=(2, 1), extra_args=(),
                 env_allow=(), spawn_env=None, min_port=None, max_port=None):
        self._project_root = resolve_project_root(project_root)
        self._python = resolve_python(python, self._project_root)
        super().__init__(
            sandbox=sandbox,
            transport=transport,
            offered=offered,
            extra_args=extra_args,
            env_allow=env_allow,
            spawn_env=spawn_env,
            min_port=min_port,
            max_port=max_port,
        )

    # ---- overrides ----
    def spawn_argv(self):
        return [self._python, "-B", "-m", EV1H_MODULE, "serve", *self.extra_args]

    def spawn_cwd(self):
        return self._project_root

    def digest_path(self):
        return os.path.join(self._project_root, "harness", "dmh_serve.py")

    # ---- lifecycle ----
    def launch(self):
        if not os.path.isfile(self._python) or not os.access(self._python, os.X_OK):
            raise HarnessError(
                "RUNTIME_FAULT",
                f"EV1H interpreter not executable: {self._python}",
            )
        if not os.path.isdir(os.path.join(self._project_root, "harness")):
            raise HarnessError(
                "RUNTIME_FAULT",
                f"EV1H project root {self._project_root} lacks harness/ package",
            )
        return super().launch()

    # capabilities() is deliberately NOT overridden: ChildRuntime returns the
    # capabilities the runtime actually reported at initialize. Pinning a
    # static dict here would make the host trust this file over the runtime,
    # and a shim that changed its own surface would go undetected.


def register(host, runtime_id="ev1h-007", **kwargs):
    """Register EV1HRuntime with a host. Convenience for host.register().

    EV1H_CAPABILITIES is passed as the *declared* set, so the registry can be
    introspected before a spawn; the bind gate still uses what the runtime
    reports for itself.
    """
    host.register(
        runtime_id,
        lambda: EV1HRuntime(sandbox=host.sandbox, **kwargs),
        declared_caps=EV1H_CAPABILITIES,
    )