# Current design

## Pipeline

`run.process_one` validates one media item, downloads it through the installed interpreter's yt-dlp,
invokes an isolated Demucs worker, and publishes one drums/drumless pair. Collections and active live
streams are rejected before download. Deno is resolved from the same Python environment.

`demucs_worker` uses the Demucs API and writes its raw estimates as float32 WAV with soundfile.
There is no per-stem rescaling or clamping. The default and only exposed model is `htdemucs`.

`audio` validates matching frame count, sample rate and channels, then sums every non-drum stem.
It processes these files in blocks, rejects non-finite samples, and applies one shared gain to
both final outputs. WAV remains float32. MP3 uses 320 kbps encoding and a decoded, 4x-oversampled
peak check targeting -1 dBFS; any additional attenuation is shared and re-encoded from the raw pair.

## Output transaction

Exports live at `<output>/<stable-id>/<format>/`. That directory contains both audio files and
`metadata.json`, including a generation id and SHA-256 checksums.

A per-track/format lock prevents overlapping publishers. All output files are completed in a
sibling staging directory. A first publication is one directory rename. Replacement renames the
old directory to a fixed backup, then renames the staged directory into place. A failed second
rename restores the backup; an interrupted swap is recovered before the next attempt.

Readers never receive a mixture of files from a partially published directory. During replacement
there can be a short interval with no directory. Authenticated downloads check the generation
under the same lock and return 409 if the result is being replaced or superseded.

Existing outputs are reusable only when their processing version, identity, format and checksums
match. Cache lookup precedes network/model work. Metadata and audio are published together.

## Queue and lifecycle

The CLI or backend holds an exclusive process lock for the lifetime of queue ownership. Concurrent
standalone CLI/backend writers are refused rather than silently overwriting one another. Revision
checks also reject stale direct saves.

Job records retain the stable id, source URL, options, status, attempts, error and output generation.
States are `pending`, `running`, `done`, `skipped`, `error`, and `cancelled`. A legacy JSON done-set
is readable, but new error records are not treated as successful completions.

Explicit submission requeues terminal jobs and captures the requested settings. Pending/running
duplicates are ignored. Server errors require explicit retry; interrupted running jobs resume after
restart. CLI invocation without inputs retries failed jobs once, not in an infinite loop.

The backend persists `running` before starting expensive work. Progress stages remain in memory.
Subprocesses run in owned process groups; cancellation terminates the owned tree, cleans staging
files and returns interrupted work to the queue. Shutdown joins the worker before releasing ownership.
Queue persistence errors are exposed, not converted into successful acceptance.

## Local API and browser

API version 2 uses finite JSON snapshots. The backend owns all durable state; the extension service
worker handles requests on demand, while the popup polls only during its lifetime.

A private per-user token is required for jobs, status and downloads. The unauthenticated health probe
contains no paths or credentials. Origin and Host checks are independent of authentication. Result
URLs refer to known completed artifacts, never arbitrary client-supplied filesystem paths.

The extension renders server text with DOM text APIs, shows failed HTTP requests, and uses the
browser download manager with an Authorization header. Tokens are not placed in result URLs.
The optional userscript is a limited status/enqueue helper, not a second full application.

## Installed environment

CLI, backend, doctor and service commands share configuration helpers. Defaults use per-user
application data and `~/Music/jamly`, not whichever directory launched the service.

`service` renders OS-specific registration definitions around the exact installed Python executable.
Windows uses a per-user logon task, macOS a LaunchAgent, and Linux a user systemd unit. Browser assets
are included in the wheel and can be copied to a stable directory for unpacked loading.

Development and CI use `uv.lock`. Fast tests exercise error/recovery contracts without downloading a
model; a separate integration test runs the real installed pipeline on generated audio. Broader
model support, native desktop packaging and marketplace distribution remain out of scope.
