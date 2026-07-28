# OpenLab Device Plugin examples

Temperature controllers, magnet supplies, and read-only monitors differ in
protocol, safety behavior, status model, and sometimes user interface.
Therefore this template provides only fail-closed examples and interface
patterns, not ready-to-use drivers for every device. Each folder under
`plugins/` is independently installable and has its own
`device.toml` and backend source. `requirements.lock` and local `wheels/` are
only needed for dependencies not supplied by the OpenLab Control framework.

The example backends contain no real protocol commands. The controller fails
closed until identity checks, bounded protocol timeouts, fresh readback, hold,
and instrument-side safety validation have been implemented. The monitor
example is read-only.

## Manual offline installation

1. Review one plugin folder and any additional dependency wheel hashes.
2. Copy it to `OpenLabControl/device_plugins/<plugin-id>/`.
3. Change only the selected device's `plugin = "<plugin-id>"` in
   `configs/default.toml`; keep safety limits, role, and timeouts in that same
   device entry.
4. Restart OpenLab Control and approve the first-load trust prompt.
5. PySide6, QtAwesome, packaging, PyVISA, and typing_extensions use the
   framework versions. Only additional dependencies are prepared from local
   wheels when prompted.

A copied plugin is not enough to activate control. A device must also select
the plugin in configuration. For each temperature/field kind there is at most
one `role = "primary"` controller; additional devices default to read-only
monitoring. Every configured device instance runs in its own child process.

## Real-instrument gate

Before a plugin is allowed near hardware, independently test:

- connect performs model/firmware identity verification without changing output;
- every transport read/write has a timeout shorter than the framework timeout;
- limits are checked in the core, the plugin, and preferably hardware interlocks;
- `hold()` uses a fresh readback and never substitutes a guessed or zero target;
- ambiguous write timeout is never automatically replayed;
- a lost read link retries, pauses SEQ timing, and faults after one minute;
- disconnect, process termination, and application exit release handles;
- secrets and site-specific addresses remain outside Git.
