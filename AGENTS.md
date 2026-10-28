# Contributing

- Application code is under `src/jamly`; Python tests are under `tests`.
- Use package imports (`from .core import ...` internally, `from jamly.core import ...` in tests).
- Install with `uv sync --locked`. Run pytest, unittest and Ruff through `uv run --locked`.
- Browser code is under `browser/`; its Node tests run with `node --test`.
- Keep fast tests offline and deterministic. Use temporary directories and explicit process cleanup.
- Test external-tool contracts with real audio in the dedicated integration check, not only mocked
  command strings. Put each unittest assertion on its own statement.
- Preserve source levels, paired-output integrity, durable job states, and explicit error reporting.
- Keep subprocess arguments as lists. Never put secrets into source, logs, URLs, or shell commands.
- Keep README focused on install/use and DESIGN focused on current behavior. Git history records
  superseded designs.
- Do not commit environments, model weights, output audio, logs, or temporary probes.
