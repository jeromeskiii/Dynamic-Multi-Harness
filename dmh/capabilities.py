"""Capability algebra (design doc, section 4).

Capabilities are booleans. The intersection of host and runtime capabilities
is what a session may use. Omitted = unsupported = false, never default-true.
Fail-closed: a preset is a required-capability set, not a string on a tool.
"""

HOST_CAPABILITIES = {
    "log.append": True,
    "log.replay": True,
    "fs.world": True,
    "sandbox.world": True,
    "approval.ask": True,
    "credentials.handle": True,
    "cancel.fuse": True,
    "session.fork": True,
    "runtime.switch": True,
}

RUNTIME_CAPABILITY_KEYS = (
    "streaming",
    "tools.native",
    "tools.code",
    "subagents.native",
    "plan",
    "goals",
    "mcp.client",
    "approval.ask",
    "compaction",
    "replay.from_log",
    "prompt.image",
    "runtime.multiSession",
    "fs.world",
    "sandbox.world",
)


def normalize(caps):
    """Omitted capability = unsupported (false), never default-true."""
    return {k: bool(v) for k, v in (caps or {}).items()}


def effective(host_caps, runtime_caps):
    """effective(c) = hostCapabilities[c] AND runtimeCapabilities[c] for keys
    the host tracks (fs.world, sandbox.world, approval.ask, ...). Runtime-only
    capabilities (streaming, tools.native, compaction, ...) are the runtime's
    own value: the host has no opinion on them."""
    host = normalize(host_caps)
    rt = normalize(runtime_caps)
    eff = {}
    for key, value in rt.items():
        if key in host:
            eff[key] = bool(value) and bool(host.get(key, False))
        else:
            eff[key] = bool(value)
    return eff


def missing_capabilities(required, caps):
    caps = normalize(caps)
    return [c for c in required if not caps.get(c, False)]
