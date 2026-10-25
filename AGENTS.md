# AGENTS.md — contributing & coding conventions

Guidance for anyone adding or changing code in this repo. Keep it about *how to work here*, not the design rationale (that lives in `DESIGN.md`) or usage (`README.md`).

## Layout (structural only — not a checklist)

All application code is a proper setuptools src-layout package under `src/splitfy/`; tests are in `tests/`. The browser extension ships separately as static assets (`browser/extension/` + the standalone Tampermonkey panel), so keep those files out of `src/`/`tests/` and never import them from the suite.

Do not maintain an exact list of member names here — it goes stale as modules are added or removed; refer to the package via its real path (`src/splitfy`, see Imports) rather than by enumerating members.

## Imports — do not use bare-sibling imports

Every import across modules inside the package must be **relative** or **absolute**; never a
bare sibling name that only works when the project root is on `sys.path`:

- Within a module: `from .core import ...`, `import .queue as q`.
- Across modules (e.g. from a test): `from splitfy.core import ...`,
  `mock.patch("splitfy.core.detect_engine", ...)`.

Reason: tests load the package via its real path (`src/`), so bare sibling imports break under
a proper loader. The suite is green under both runners only when this holds (see Testing).

## Do not shadow stdlib — in particular `queue`

This box's Python ships a stdlib `queue` that **lacks the CPython watch_queue extension**
(no `WatchQueue`). Code must therefore import our own implementation directly:

```python
from splitfy.watch_queue import WatchQueue, default_queue_path
```

Never name a module `queue.py`, and never rely on repo-root shadowing to make `from queue import` work.

> This file is deliberately free of hard-coded counts (pass numbers, module lists). Those drift the moment something changes; replace them with behavior-based checks instead. See Testing rules below for how.

## Testing rules (run BOTH runners)

The suite must pass under **both** of these; do not let it only work under one:

```bash
python -m pytest                    # pythonpath=["src"] from pyproject.toml
python -m unittest discover -s tests  # each test file shims src onto sys.path
```

- Every test passes under both runners, and no test is skipped except the two SSE-integration
  methods noted below (which skip by design). If either runner reports failures or skips, that is
  wrong — do not paper over it by editing any count in this file; fix the cause.
- Tests must be **portable across machines**, never dependent on this box's Python or PATH. Mock
  any environment-specific lookup (locating an interpreter, scanning a portable yt-dlp cache) with
  `mock.patch`; do not call code that relies on host filesystem/PATH state and then assert real results.
  A test green only because of something unique to this machine will silently break elsewhere — write it so the box is irrelevant.

- Each test module imports the package via its absolute name and adds a single line to put
  `src/` on `sys.path`; it must not add the project root or any redundant inserts.
- `mock.patch` targets use **full dotted paths** (`"splitfy.core.X"`), never bare `"core.X"`.
- Source stays ASCII-only so it compiles and loads under any locale; keep `py_compile` clean.

### The two skipped SSE-integration tests are intentional

Racing an infinite streaming worker thread against one long-lived socket read is inherently
flaky under loose daemon threads, so both integration methods in `test_server.py` skip cleanly
instead of hanging/racing. A bounded-stream harness (e.g. FastAPI-style `TestClient`) can drive
them later; the reusable helpers (`_read_sse_currents`, `_post_add`, `_FakeResult`) are retained for that.

## Hygiene

- Commit messages use an **inline, single-quoted `git -m`**. Do not put literal backticks inside a *double*-quoted body: bash treats `` `...` `` as command substitution (it strips them and runs the inner text). Single-quoting avoids this entirely. `/tmp/...` works fine here; no alias workaround is needed for it. If you need multi-line prose, write it directly in one `-m`.
- Commit only source, tests, and docs. `.portable/`, downloaded models/cache, and stray temp dirs are gitignored — never commit them.
- Do not leave diagnostic/probe scripts in the tree; clean up before committing.
