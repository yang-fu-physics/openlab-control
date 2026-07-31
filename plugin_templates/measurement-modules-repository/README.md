# OpenLab Measurement Modules repository

This is the Git-ready layout for the single shared Measurement Module
repository. Keep every independently installable module under `modules/<id>/`.
The included `simulated_transport` module is hardware-free and serves as the
reference implementation and test fixture.

## Manual offline installation

1. Review the module source and `module.toml`. Review `requirements.lock` and
   wheels only when the module has dependencies not supplied by the framework.
2. Copy one complete module folder to `OpenLabControl/modules/<id>/`.
3. PySide6, QtAwesome, packaging, PyVISA, and typing_extensions use the exact
   versions supplied by OpenLab Control. Do not duplicate those wheels.
4. For additional dependencies only, include every required Windows wheel
   under the module's `wheels/` folder (or the application's shared `wheels/`
   folder). Restart OpenLab Control and use `Install Dependencies` when shown.
   Network fallback is intentionally unavailable.
5. Enable the module and approve the first-load trust prompt.
6. Verify that saved settings are loaded but not applied until the operator
   chooses `Apply Settings`.

Never commit `module_data`, acquired DAT files, instrument addresses containing
secrets, or generated `plugin_runtime` contents. A module owns its measurement
instruments, runs its backend in one child process, and may only read
temperature/field/monitor snapshots supplied by the core. Use
`context.sample_system()` for a fresh snapshot and
`context.interruptible_sleep()` for pause/stop-aware timing.

## Required release checks

- Validate manifest ID, API/core range, fixed columns, and source entry points.
- Exercise initialize/apply/begin/measure/end/abort and every error path.
- Verify Warning deduplication and Error termination.
- Test explicit scheduling mode, one row per logical channel slot, and
  same-slot parallel execution with another module.
- Test bounded driver and framework timeouts plus forced worker cleanup.
- Verify framework dependency ranges, and test any additional offline wheel set
  on the target Windows/Python architecture.
- Increment `version` whenever shipped content or dependencies change.
