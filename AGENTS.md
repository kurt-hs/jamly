# Contributing

- Application code is under `src/jamly`; Python tests are under `tests`.
- Use package imports (`from .core import ...` internally, `from jamly.core import ...` in tests).
- Install with `uv sync --locked`. Run pytest, unittest and Ruff through `uv run --locked`.
- Browser code is under `browser/`; its Node tests run with `node --test`.
- The companion page is under `src/jamly/web`; it is an exporter, not a player.
- `tools/` builds native desktop archives. Use `uv sync --locked --group bundle` and then
  `uv run --no-sync` for packaging commands so the build dependencies are not pruned mid-build.
- Exercise relocated bundles with an empty PATH and both models. Keep native sources, notices
  and checksums alongside distribution artifacts; never commit binaries or weights.
- Keep authentication out of URLs. The companion's automatic pairing uses a private, single-use file.
- Keep fast tests offline and deterministic. Use temporary directories and explicit process cleanup.
- Test external-tool contracts with real audio in the dedicated integration check, not only mocked
  command strings. Put each unittest assertion on its own statement.
- Preserve source levels, paired-output integrity, durable job states, and explicit error reporting.
- Keep subprocess arguments as lists. Never put secrets into source, logs, URLs, or shell commands.
- Keep README focused on install/use and DESIGN focused on current behavior. Git history records
  superseded designs.
- Do not commit environments, model weights, output audio, logs, or temporary probes.
