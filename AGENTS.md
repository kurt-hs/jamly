# AGENTS.md — contributing & coding conventions

Guidance for anyone adding or changing code in this repo. Keep it about *how to work here*, not the design rationale (that lives in `DESIGN.md`) or usage (`README.md`).

## Layout

All application code is a proper package under `src/auto_convert/`:

```
src/auto_convert/{__init__,bootstrap,core,normalize,provision,run,server,_input_queue}.py
tests/*.py            # unittest suite (also runs under pytest)
DESIGN.md               design + portability research (read-only reference)
README.md             user-facing overview & quick start
AGENTS.md             this file
```

`src/` is the setuptools src-layout root (`pyproject.toml`). The package dir is `auto_convert`.

The browser extension source lives under `browser/` (relocated from the repo root, §13):
its MV3 manifest + service worker + popup/options HTML and the shared DOM-free client are in
`browser/extension/`; the standalone Tampermonkey panel (`auto_convert.user.js`) is in
`browser/userscripts/`. It is not a Python package — it ships as static assets, so keep those
files out of `src/`/`tests/` and never `import` them from the suite.

## Imports — do not use bare-sibling imports

Every import across modules inside the package must be **relative** or **absolute**; never a
bare sibling name that only works when the project root is on `sys.path`:

- Within a module: `from .core import ...`, `import .queue as q`.
- Across modules (e.g. from a test): `from auto_convert.core import ...`,
  `mock.patch("auto_convert.core.detect_engine", ...)`.

Reason: tests load the package via its real path (`src/`), so bare sibling imports break under
a proper loader. The suite is green under both runners only when this holds (see Testing).

## Do not shadow stdlib — in particular `queue`

This box's Python ships a stdlib `queue` that **lacks the CPython watch_queue extension**
(no `WatchQueue`). Code must therefore import our own implementation directly:

```python
from auto_convert._input_queue import WatchQueue, default_queue_path
```

Never name a module `queue.py`, and never rely on repo-root shadowing to make `from queue import` work.

## Testing rules (run BOTH runners)

The suite must pass under **both** of these; do not let it only work under one:

```bash
python -m pytest                    # pythonpath=["src"] from pyproject.toml
python -m unittest discover -s tests  # each test file shims src onto sys.path
```

Expected result: **129 passed under both runners** (no skips; the SSE-integration tests that
were once deemed flaky now pass cleanly).

- Each test module imports the package via its absolute name and adds a single line to put
  `src/` on `sys.path`; it must not add the project root or any redundant inserts.
- `mock.patch` targets use **full dotted paths** (`"auto_convert.core.X"`), never bare `"core.X"`.
- Source stays ASCII-only so it compiles and loads under any locale; keep `py_compile` clean.

### The two skipped SSE-integration tests are intentional

Racing an infinite streaming worker thread against one long-lived socket read is inherently
flaky under loose daemon threads, so both integration methods in `test_server.py` skip cleanly
instead of hanging/racing. A bounded-stream harness (e.g. FastAPI-style `TestClient`) can drive
them later; the reusable helpers (`_read_sse_currents`, `_post_add`, `_FakeResult`) are retained for that.

## Hygiene

- Commit only source, tests, and docs. `.portable/`, downloaded models/cache, and stray temp dirs are gitignored — never commit them.
- Do not leave diagnostic/probe scripts in the tree; clean up before committing.
