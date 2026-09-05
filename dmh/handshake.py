"""Layer 0: process identity (design doc, section 1-2).

Cookie mismatch is UX, not auth. ABI majors are negotiated on the handshake
line for socket transports and at initialize for stdio. Minors never appear
on the line - they live in capabilities.
"""

import hashlib
import os
import re
import tempfile
import uuid

MAGIC_COOKIE_KEY = "HARNESS_PLUGIN_MAGIC_COOKIE"
PROTOCOL_VERSIONS_KEY = "HARNESS_PLUGIN_PROTOCOL_VERSIONS"
TRANSPORT_KEY = "HARNESS_PLUGIN_TRANSPORT"
UNIX_SOCKET_DIR_KEY = "HARNESS_PLUGIN_UNIX_SOCKET_DIR"
MIN_PORT_KEY = "HARNESS_PLUGIN_MIN_PORT"
MAX_PORT_KEY = "HARNESS_PLUGIN_MAX_PORT"
INSTANCE_ID_KEY = "HARNESS_PLUGIN_INSTANCE_ID"

# Frozen for the life of the ABI family. Never rotate this to "fix security";
# rotate trust pins instead.
MAGIC_COOKIE = "7f3c2a91e0b64d18c5a9f2d47b81e6c03d5a17b94e2c8f60a1d3b7c9e4f20815"

# Process exit codes (design doc, section 7.1)
EXIT_OK = 0
EXIT_BOOT = 1
EXIT_COOKIE = 2
EXIT_ABI = 3
EXIT_BIND = 4
EXIT_CERT = 5

# Host clocks (design doc, section 1.3)
HANDSHAKE_TIMEOUT = 5.0
INITIALIZE_TIMEOUT = 10.0
CONFIGURE_TIMEOUT = 30.0
STOP_TIMEOUT = 5.0

# Ambient secrets the child must never inherit (design doc, section 1.2)
FORBIDDEN_EXACT = frozenset({"SSH_AUTH_SOCK", "AWS_PROFILE"})
FORBIDDEN_PREFIXES = ("AWS_", "OPENAI_", "GH_", "GITHUB_")
FORBIDDEN_SUBSTRINGS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "[::1]", "localhost"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class HandshakeFailure(Exception):
    """Host-side Layer-0 parse/validation failure."""

    def __init__(self, code, message, detail=""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail


def forbidden_env_key(key):
    if key in FORBIDDEN_EXACT:
        return True
    if key.startswith(FORBIDDEN_PREFIXES):
        return True
    return any(sub in key for sub in FORBIDDEN_SUBSTRINGS)


def child_env(*, versions, transport, instance_id=None, socket_dir=None,
              allow=(), spawn_env=None, min_port=None, max_port=None):
    """Build the child environment: allowlist only, no ambient secrets."""
    env = {"TMPDIR": os.environ.get("TMPDIR") or tempfile.gettempdir()}
    for key in allow:
        if key in os.environ:
            env[key] = os.environ[key]
    env[MAGIC_COOKIE_KEY] = MAGIC_COOKIE
    env[PROTOCOL_VERSIONS_KEY] = ",".join(str(v) for v in versions)
    env[TRANSPORT_KEY] = transport
    env[INSTANCE_ID_KEY] = instance_id or uuid.uuid4().hex[:12]
    if transport == "unix":
        env[UNIX_SOCKET_DIR_KEY] = socket_dir or tempfile.gettempdir()
    if transport == "tcp":
        env[MIN_PORT_KEY] = str(min_port or 0)
        env[MAX_PORT_KEY] = str(max_port or 0)
    for key, value in (spawn_env or {}).items():
        if forbidden_env_key(key):
            raise HandshakeFailure(
                "RUNTIME_FAULT", f"refusing to pass forbidden env key {key!r}"
            )
        env[key] = value
    return env


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def redact(text, limit=500):
    """Cap and redact stderr tails before they enter host logs."""
    out = str(text or "")[:limit]
    return out.replace(MAGIC_COOKIE, "[redacted-cookie]")


def select_abi(host_offered, child_supported):
    """Both sides preference-ordered; first overlap wins. Children do not
    invent majors."""
    for v in host_offered:
        if v in child_supported:
            return v
    return None


def build_handshake_line(abi, net, addr, wire="jsonrpc", cert="", mux=0, digest=""):
    return f"1|{abi}|{net}|{addr}|{wire}|{cert}|{mux}|{digest}"



def parse_handshake_line(line, host_offered, *, min_port=None, max_port=None):
    """Host-side parse of the Layer-0 line. Parse with split('|', 8); fewer
    than 5 fields is malformed, extra fields after 8 are reserved."""
    raw = line.rstrip("\n")
    parts = raw.split("|")
    if len(parts) < 5:
        raise HandshakeFailure(
            "HANDSHAKE_MALFORMED", "fewer than 5 handshake fields", raw
        )
    parts = (parts + [""] * 8)[:8]
    core, abi_s, net, addr, wire, cert, mux_s, digest = parts
    if core != "1":
        raise HandshakeFailure(
            "HANDSHAKE_MALFORMED", f"CORE must be 1, got {core!r}", raw
        )
    try:
        abi = int(abi_s)
    except ValueError:
        raise HandshakeFailure(
            "HANDSHAKE_MALFORMED", f"ABI not an integer: {abi_s!r}", raw
        ) from None
    if abi not in host_offered:
        raise HandshakeFailure(
            "ABI_UNSUPPORTED",
            f"child chose ABI {abi} not in host offered {list(host_offered)}",
            raw,
        )
    if net not in ("unix", "tcp"):
        raise HandshakeFailure("HANDSHAKE_MALFORMED", f"bad NET {net!r}", raw)
    if wire not in ("grpc", "jsonrpc"):
        raise HandshakeFailure("HANDSHAKE_MALFORMED", f"bad WIRE {wire!r}", raw)
    mux = mux_s.strip().lower()
    if mux not in ("", "0", "1", "true", "false"):
        raise HandshakeFailure("HANDSHAKE_MALFORMED", f"bad MUX {mux_s!r}", raw)
    if mux in ("1", "true") and wire != "grpc":
        raise HandshakeFailure(
            "BROKER_MUX_UNSUPPORTED", "MUX=1 requires WIRE=grpc", raw
        )
    if net == "unix":
        if not addr.startswith("/") or ".." in addr:
            raise HandshakeFailure(
                "HANDSHAKE_MALFORMED", f"bad unix ADDR {addr!r}", raw
            )
    else:
        host, _, port_s = addr.rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_s)
        except ValueError:
            raise HandshakeFailure(
                "HANDSHAKE_MALFORMED", f"bad tcp ADDR {addr!r}", raw
            ) from None
        if host not in _LOOPBACK_HOSTS:
            raise HandshakeFailure(
                "BIND_NOT_LOOPBACK", f"tcp bind {host!r} is not loopback", raw
            )
        if min_port is not None and port < min_port:
            raise HandshakeFailure(
                "HANDSHAKE_MALFORMED",
                f"port {port} below min {min_port}",
                raw,
            )
        if max_port is not None and port > max_port:
            raise HandshakeFailure(
                "HANDSHAKE_MALFORMED",
                f"port {port} above max {max_port}",
                raw,
            )
    if cert and len(cert) <= 50:
        raise HandshakeFailure("HANDSHAKE_MALFORMED", "CERT stub too short", raw)
    if digest and not _HEX64.match(digest):
        raise HandshakeFailure(
            "HANDSHAKE_MALFORMED", "DIGEST must be lowercase hex sha256", raw
        )
    return {
        "core": 1,
        "abi": abi,
        "net": net,
        "addr": addr,
        "wire": wire,
        "cert": cert,
        "mux": mux in ("1", "true"),
        "digest": digest,
    }
