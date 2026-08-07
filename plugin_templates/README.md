# Extension repository templates

These directories are Git-ready starting points. They are not active
extensions and are never scanned in place.

- `measurement-modules-repository/`: one shared repository containing all
  Measurement Modules that may be distributed to users.
- `device-plugins-repository/` contains only fail-closed examples. Temperature,
  field, and monitor devices differ in protocol, safety behavior, and user
  interface, so the core does not claim to provide ready-to-use drivers for
  every instrument.

To install an extension without network access, copy one complete folder from
the repository's `modules/` or `plugins/` directory into the matching
`OpenLabControl/modules/` or `OpenLabControl/device_plugins/` directory, then
restart OpenLab Control. Do not copy repository metadata, tests, secrets, or
development environments into the application directory.

The first load asks the operator to trust the extension's exact type, ID,
version, and content fingerprint. Any later source or wheel change requires a
new confirmation. Dependencies are installed only from hashed lock files and
local wheels into an isolated per-extension runtime.
