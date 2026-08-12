# Repository templates

These are two independent, Git-ready starting points. OpenLab Control never
scans or runs code directly from `templates/`.

- `measurement-modules-repository/` is a shared repository layout for
  Measurement Modules that users enable as needed.
- `system-instruments-repository/` contains fail-closed System Instrument
  examples for temperature, field and read-only monitoring.

Install one complete folder offline:

```text
repository/modules/<module-id>/       -> OpenLabControl/modules/<module-id>/
repository/instruments/<instrument-id>/ -> OpenLabControl/system_instruments/<instrument-id>/
```

Do not copy repository metadata, tests, secrets or development environments
into the application directory. First load asks the operator to trust the
exact type, ID, version and content fingerprint. Extra dependencies are
installed only from hashed lock files and local wheels into an isolated
runtime.
