"""Presets are required-capability sets, not strings on a tool (doc section 4).

Bind fails closed: if the chosen runtime lacks a required capability, the
selection fails with CAPABILITY_REQUIRED. You do not "almost run" a coding
preset without a sandbox world.
"""

PRESETS = {
    "default": {"requires": ("streaming",)},
    "coding": {"requires": ("streaming", "tools.native", "sandbox.world")},
    "research": {"requires": ("streaming", "compaction", "replay.from_log")},
}


def preset_requires(name):
    if name not in PRESETS:
        raise KeyError(f"unknown preset: {name!r}")
    return list(PRESETS[name]["requires"])
