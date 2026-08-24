# OpenLab System Instrument examples

Temperature controllers, magnet supplies and read-only monitors differ in
protocol, safety behavior and status model. This template therefore provides
only fail-closed patterns, not ready-to-use drivers for arbitrary instruments.
Each folder under `instruments/` is independently installable and contains its
own `instrument.toml` and backend source.

The controller example contains no real protocol commands and deliberately
fails before hardware use. The monitor example is read-only.

Backends use ordinary synchronous methods:
`open/read_status/read_measurement/set_target/hold/execute_sequence_command/close`. Read methods return
plain dictionaries. Reading labels, units and precision belong only in
`instrument.toml`; the required `[panel]` table selects `controller`, `readout`,
or `switch`. The core builds timestamps and runtime snapshots. A `readout` panel
shows up to four readings in a 2x2 grid; a fifth reading starts the next panel
to its right. A `switch` panel displays a 0/1 main reading and uses all declared
no-parameter `sequence_commands` as buttons. Controller appearance remains unchanged.

## Manual offline installation

1. Review one instrument folder and any extra dependency wheel hashes.
2. Copy it to `OpenLabControl/system_instruments/<instrument-id>/`.
3. Select its scanned resource with `resource = "<resource-id>"` in a local
   configuration; keep control permission, limits and timeouts in that
   `[[instruments]]` entry.
4. Restart OpenLab Control and approve the first-load trust prompt.
5. Only framework-external dependencies need local wheels. PySide6,
   QtAwesome, packaging, PyVISA and typing_extensions use framework versions.

Copying a folder alone does not activate it. A configured instrument instance
must select its resource. Each configured instance runs in its own child
process, and each temperature/field kind allows at most one instance with
`control_enabled = true`.

## Real-instrument gate

Before a System Instrument is allowed near hardware, independently test:

- `open()` verifies model/firmware without changing output;
- every transport call has a timeout shorter than the framework timeout;
- limits are checked by the core, the backend and preferably hardware interlocks;
- `hold()` uses a fresh readback instead of a guessed or zero target;
- an ambiguous write timeout is never automatically replayed;
- lost read links retry for the configured recovery window and then fault;
- partial open, `close()`, process termination and application exit release handles;
- secrets and site-specific addresses remain outside Git.
