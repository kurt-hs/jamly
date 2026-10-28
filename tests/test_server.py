"""HTTP status-server integration tests.

These drive the live ``ItemServer`` over real HTTP and observe per-item completion through the
streaming ``/status`` SSE endpoint while a background worker processes items concurrently.

Why these are robust (and do NOT hang or race):

* No stdlib-``queue`` shadowing: since the restructure, ``server.py`` imports our own
  :class:`jamly.watch_queue.WatchQueue` (which now also exposes ``done_ids()`` and an
  ``.items`` accessor). The HTTP layer therefore loads cleanly under both runners -- this is the
  resolution of the earlier "queue issue" that broke pytest collection. Tests use only the standard
  library, so they run anywhere without third-party HTTP clients.
* Bounded stream, not a long-lived socket race: instead of one reader holding an infinite SSE
  connection while racing against the worker loop (which stalls under loose daemon threads), each
  test opens a fresh short-lived ``urllib`` connection per poll and reads at most a bounded number
  of bytes -- streaming until every added item shows up in ``last_completed``. A fast-finisher's
  completion is reliably observable because the worker floors its idle sleep at 0.05s, always
  yielding to the HTTP handler so each write flushes promptly. This mirrors FastAPI's TestClient
  pattern: a bounded stream read, never an infinite one racing an unbounded worker.

The heavy steps (yt-dlp + ffmpeg/GPU) are stubbed out with :func:`unittest.mock.patch.object` on
``jamly.server.process_one`` so the tests add no network dependency and finish deterministically.

Core queue behaviour this depends on is covered by :file:`tests/test_queue.py`; here we cover the live
HTTP/SSE path end to end.
"""

import os
import tempfile
import uuid
import json
import threading
import time
import unittest
from unittest import mock
from urllib.request import Request, urlopen
from urllib.error import URLError
import http.client

from jamly.server import ItemServer, _is_loopback_host, build_parser


class _FakeResult:
    """Stands in for :func:`run.process_one`'s return value during live-stream tests."""

    def __init__(self, ok=True, stage="mix", title=None, error=None):
        self.ok = ok
        self.stage = stage
        self.title = title
        self.error = error


def _stub_process_one(result):
    """Patch ``server.process_one`` to return a fixed fake result (no network).

    Returns a context manager. The server looks up ``process_one`` in its module globals at call
    time, so patching it here makes every worker cycle use our stub instead of real yt-dlp/ffmpeg.
    """
    def _fake(wid, args):
        return result

    return mock.patch.object(_server_module(), "process_one", side_effect=_fake)


def _server_module():
    # import lazily so the patch can target the fully-loaded module object
    from jamly import server as _smod
    return _smod


def _start_server(idle_wait_s=0.0, token=None):
    """Construct an ItemServer bound to a free ephemeral port; start its worker AND HTTP serving.

    Returns (server, port). ``ItemServer.start()`` launches the queue-worker thread and blocks in
    ``serve_forever();`` we run it from a dedicated daemon thread so the test body can finish while
    both loops keep running. Each instance gets its own isolated queue file in a temp dir so tests
    never share state via the default cwd queue path. Pass ``token`` to spin up a server that gates
    POST /add with a shared bearer secret (S2 hardening).
    """
    # pure string path (never create+remove an on-disk file -> no Windows handle-lock race).
    # from_path() treats a missing file as an empty queue until .save() is called.
    qpath = os.path.join(tempfile.gettempdir(), "autocv-test-queue-%s.json" % uuid.uuid4().hex)
    argv = ["--port", "0", "--idle-wait-s", str(idle_wait_s), "--queue", qpath]
    if token is not None:
        argv += ["--server-token", token]
    args = build_parser().parse_args(argv)
    srv = ItemServer(args)  # binds eagerly; the assigned port is in _server.server_address[1]
    # daemon=True so a lingering/failed teardown can never block pytest from exiting.
    worker_t = threading.Thread(target=srv._loop, name="test-worker", daemon=True)
    server_t = threading.Thread(target=srv._server.serve_forever, name="test-server", daemon=True)
    worker_t.start()
    server_t.start()  # serve_forever blocks this thread until stop(); captured for clean join
    # serve_forever is running on server_t; mark serving so stop() does a bounded shutdown()
    # (waits for the poller to signal) instead of closing the socket mid-poll.
    srv._serving = True
    return srv, srv._server.server_address[1], qpath, worker_t, server_t


def _add_items(base_url, inputs, titles=None):
    """POST a JSON ``/add`` request; return the parsed response body."""
    data = json.dumps({"inputs": list(inputs), "titles": list(titles or [])}).encode("utf-8")
    req = Request(base_url + "/add", data=data)
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=10) as resp:  # context manager (http.client); safe on Windows
        return json.loads(resp.read().decode("utf-8"))


def _post(port, inputs=None, titles=None, auth=None):
    """POST ``/add`` via http.client; return ``(status, parsed_body)`` and never raise on non-2xx."""
    data = json.dumps({"inputs": list(inputs or []), "titles": list(titles or [])}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth is not None:
        headers["Authorization"] = auth
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        c.request("POST", "/add", body=data, headers=headers)
        r = c.getresponse()
        raw = r.read().decode("utf-8", "replace")
        return r.status, (json.loads(raw) if raw.strip() else None)
    finally:
        c.close()


def _poll_status(base_url):
    """One bounded read of ``GET /status``; return the list of SSE JSON events seen in this poll.

    Reads line-by-line until at least one complete SSE event has been consumed, then stops -- so a
    *paced* stream (events spaced by ``--interval-ms``) stays time-bounded via the socket timeout
    instead of blocking until a fixed 64KB buffer fills (which a paced stream of small events never
    does). Each poll opens a fresh short-lived connection; the server re-streams cumulative
    ``last_completed`` on every tick, so repeated polls observe monotonic progress. Returns [] if
    nothing readable yet.
    """
    req = Request(base_url + "/status")
    try:
        with urlopen(req, timeout=5) as resp:
            events = []
            line = resp.readline()
            while line:
                text = line.decode("utf-8", "replace").strip()
                if text.startswith("data: "):
                    try:
                        events.append(json.loads(text[len("data: ") :]))
                    except (ValueError, TypeError):  # noqa: BLE001 - skip malformed chunks defensively
                        pass
                    # stop after the first complete event so a paced stream stays time-bounded
                    if events:
                        break
                line = resp.readline()
    except (URLError, OSError):  # noqa: BLE001 - transient; the caller keeps polling
        return []
    return events


def _completed_ids(events):
    """Watch ids that have reached a terminal record in any streamed event."""
    out = []
    for e in events:
        for rec in e.get("last_completed", []) or []:
            if rec["watch_id"] not in out:
                out.append(rec["watch_id"])
    return out


def _completed_records(events):
    """Terminal records (done/error) across streamed events, deduped by watch_id.

    ``last_completed`` grows monotonically with one terminal record per finished item and is re-
    streamed on every tick; across many polls this helper collapses them to a single representative
    record per id so counts are stable regardless of how long we polled.
    """
    seen = set()
    out = []
    for e in events:
        for rec in e.get("last_completed", []) or []:
            if rec["watch_id"] not in seen:
                seen.add(rec["watch_id"])
                out.append(rec)
    return out


def _cleanup(srv, qpath, threads):
    """Best-effort teardown of a server instance and its temp queue file."""
    try:
        srv.stop()  # sets the stop flag so both loops exit on their next check
    except Exception:  # noqa: BLE001 - best-effort teardown
        pass
    # let any in-flight .save() unwind before removing the queue file (Windows handle race).
    for t in threads:
        try:
            if t.is_alive():
                t.join(timeout=2.0)
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
    try:
        os.remove(qpath)
    except OSError:  # noqa: BLE001 - already gone or never created
        pass


def _wait_until(port, predicate, deadline=8.0, interval=0.05):
    """Poll ``GET /status`` until predicate(events) is true; raise AssertionError on timeout."""
    base_url = "http://127.0.0.1:%d" % port
    deadline_at = time.time() + deadline
    while time.time() < deadline_at:
        events = _poll_status(base_url)
        if predicate(events):
            return events
        time.sleep(interval)
    raise AssertionError("deadline exceeded waiting for server condition to hold")


class ServerIntegrationTests(unittest.TestCase):

    def test_sse_streams_progress_then_completes_all_items(self):
        """Add two items; stream /status until both appear in ``last_completed`` with status done."""
        wid_a, wid_b = "aBcDeFg1234", "zYxWvUt5678"
        titles = {"aBcDeFg1234": "Song A", "zYxWvUt5678": "Song B"}
        with _stub_process_one(_FakeResult(ok=True, stage="mix", title="Song A")):
            srv, port, qpath, worker_t, server_t = _start_server()
            threads = (worker_t, server_t)
            try:
                # add while the stub is active so the worker processes items without touching network
                added = _add_items("http://127.0.0.1:%d" % port, [wid_a, wid_b], [titles[wid_a], titles[wid_b]])
                self.assertEqual(added["newly_added"], 2)
                self.assertEqual(added["pending_count"], 2)

                # stream until both terminal records are visible (bounded read; does not hang)
                events = _wait_until(port, lambda evs: {wid_a, wid_b} <= set(_completed_ids(evs)))
                recs = _completed_records(events)
                self.assertEqual({r["watch_id"] for r in recs}, {wid_a, wid_b})

                # every terminal record is 'done' with the expected title/stage, no error text
                for rec in recs:
                    self.assertIn(rec["status"], ("done",))
                    self.assertNotIn("error", str(rec.get("title") or ""))
            finally:
                _cleanup(srv, qpath, threads)

    def test_sse_reports_error_status_when_pipeline_fails(self):
        """A failing item surfaces an 'error' terminal record and is marked done (no infinite retry)."""
        wid = "aBcDeFg1234"
        with _stub_process_one(_FakeResult(ok=False, stage="download", title=None, error="boom")):
            srv, port, qpath, worker_t, server_t = _start_server()
            threads = (worker_t, server_t)
            try:
                added = _add_items("http://127.0.0.1:%d" % port, [wid], ["Song A"])
                self.assertEqual(added["newly_added"], 1)

                events = _wait_until(port, lambda evs: wid in _completed_ids(evs))
                recs = [r for r in _completed_records(events) if r["watch_id"] == wid]
                self.assertEqual(len(recs), 1)   # exactly one terminal record -> not retried forever
                self.assertEqual(recs[0]["status"], "error")
                self.assertIn("boom", recs[0].get("error", ""))

                # it is marked done, so a later poll shows nothing new pending for this id
                events2 = _wait_until(port, lambda evs: wid in _completed_ids(evs), deadline=3.0)
                recs2 = [r for r in _completed_records(events2) if r["watch_id"] == wid]
                self.assertEqual(len(recs2), 1)
            finally:
                _cleanup(srv, qpath, threads)

    def test_worker_saves_queue_after_finishing_item(self):
        """The worker persists the queue (mark_done + save) after each item so the done-set
        survives a restart; a previously-finished item loads as done, not re-queued."""
        wid = "aBcDeFg1234"
        with _stub_process_one(_FakeResult(ok=True, stage="mix", title="Song A")):
            srv, port, qpath, worker_t, server_t = _start_server()
            threads = (worker_t, server_t)
            try:
                added = _add_items("http://127.0.0.1:%d" % port, [wid], ["Song A"])
                self.assertEqual(added["newly_added"], 1)
                # wait for the terminal record via /status ...
                _wait_until(port, lambda evs: wid in _completed_ids(evs))
                # halt the worker so no .save() races our read (on Windows you cannot os.replace a
                # file another handle currently has open), then join so it fully unwinds.
                srv.stop()
                for t in threads:
                    try:
                        if t.is_alive():
                            t.join(timeout=2.0)
                    except Exception:  # noqa: BLE001 - best-effort teardown
                        pass
                # ... the queue is now quiescent; the on-disk done-set must reflect the finished item
                import jamly.watch_queue as wq_mod
                queue = wq_mod.WatchQueue.from_path(qpath)
                self.assertIn(wid, queue.done_ids())
                self.assertNotIn(wid, queue.pending_ids())
                # and a fresh load (simulating a restart) sees it as done, not re-queued
                reloaded = wq_mod.WatchQueue.from_path(qpath)
                self.assertIn(wid, reloaded.done_ids())
                self.assertNotIn(wid, reloaded.pending_ids())
            finally:
                _cleanup(srv, qpath, threads)

    def test_cors_headers_on_responses_and_preflight(self):
        """The browser clients fetch this http://127.0.0.1 server from a cross-origin
        extension/userscript context, so every response (and the OPTIONS preflight) must carry
        CORS headers; without them the browsers would block the body and the popup/stem could not
        talk to the queue over HTTP."""
        wid = "corsTestId01"
        with _stub_process_one(_FakeResult(ok=True, stage="mix", title=wid)):
            srv, port, qpath, worker_t, server_t = _start_server()
            threads = (worker_t, server_t)
            try:
                # GET /healthz and POST /add must both advertise CORS on the wire.
                req = Request("http://127.0.0.1:%d/healthz" % port)
                with urlopen(req, timeout=5) as resp:
                    headers = dict(resp.getheaders())
                self.assertEqual(resp.status, 200)
                self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")

                data = json.dumps({"inputs": [wid], "titles": []}).encode("utf-8")
                req = Request("http://127.0.0.1:%d/add" % port, data=data)
                req.add_header("Content-Type", "application/json")
                req.add_header("Origin", "https://youtube.com")
                with urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(dict(resp.getheaders()).get("Access-Control-Allow-Origin"), "*")

                # A real cross-origin preflight (as the extension would send) must return 204 with CORS.
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    c.request(
                        "OPTIONS", "/add",
                        headers={"Origin": "https://youtube.com",
                                "Access-Control-Request-Method": "POST"},
                    )
                    r = c.getresponse()
                    hdrs = dict(r.getheaders())
                    self.assertEqual(r.status, 204)
                    self.assertEqual(hdrs.get("Access-Control-Allow-Origin"), "*")
                    self.assertIn("POST", hdrs.get("Access-Control-Allow-Methods", ""))
                finally:
                    c.close()
            finally:
                _cleanup(srv, qpath, threads)



class _LoopbackHostTests(unittest.TestCase):
    """``_is_loopback_host`` maps the bind host to a loopback/no-auth default."""

    def test_loopback_hosts_are_trusted(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertTrue(_is_loopback_host(host), "%s should be loopback" % host)

    def test_empty_host_defaults_to_loopback(self):
        # fail-safe: an empty/absent host must never silently widen to all interfaces.
        self.assertTrue(_is_loopback_host(""))

    def test_exposed_hosts_are_not_loopback(self):
        for host in ("0.0.0.0", "192.168.1.5", "[::]", "example.com"):
            self.assertFalse(_is_loopback_host(host), "%s should be treated as exposed" % host)

    def test_case_and_whitespace_insensitive(self):
        self.assertTrue(_is_loopback_host("  LOCALHOST "))
        self.assertTrue(_is_loopback_host("127.0.0.1\t"))


class ItemServerBindGuardTests(unittest.TestCase):
    """Binding off-loopback is an explicit opt-in that requires a server token (S2)."""

    def _srv(self, host="0.0.0.0", port=0, token=None):
        args = build_parser().parse_args(["--host", host, "--port", str(port),
                                          "--queue", os.path.join(tempfile.gettempdir(),
                                                                  "autocv-bind-guard-%s.json" % uuid.uuid4().hex)])
        if token is not None:
            args.server_token = token
        return ItemServer(args)

    def test_off_loopback_without_token_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._srv(host="0.0.0.0", port=0, token=None)
        self.assertIn("server-token", str(ctx.exception))

    def test_off_loopback_with_token_binds(self):
        srv = self._srv(host="0.0.0.0", port=0, token="sekret")   # must NOT raise -> binds eagerly
        try:
            self.assertEqual(srv._server.server_address[0], "0.0.0.0")
            self.assertEqual(srv._token, "sekret")
        finally:
            srv.stop()

    def test_loopback_bind_allowed_without_token(self):
        # the default path: loopback needs no token and binds cleanly.
        srv = self._srv(host="127.0.0.1", port=0, token=None)
        try:
            self.assertIn("127.0.0.1", srv._server.server_address[0])
            self.assertIsNone(srv._token)
        finally:
            srv.stop()

    def test_token_can_also_come_from_environment(self):
        # env is read at construction time; --server-token overrides it for a single instance.
        with mock.patch.dict(os.environ, {"SPLITFY_SERVER_TOKEN": "from-env"}, clear=True):
            srv = self._srv(host="0.0.0.0", port=0, token=None)   # no CLI token -> env supplies auth
            try:
                self.assertEqual(srv._token, "from-env")
            finally:
                srv.stop()
        with mock.patch.dict(os.environ, {"SPLITFY_SERVER_TOKEN": "from-env"}, clear=True):
            srv = self._srv(host="0.0.0.0", port=0, token="cli-override")
            try:
                self.assertEqual(srv._token, "cli-override")
            finally:
                srv.stop()


class S2WriteAuthTests(unittest.TestCase):
    """POST /add is gated by a bearer token when one is configured; accepted when none is."""

    TOKEN = "sekret-abc"

    def test_post_add_accepted_when_no_token_configured(self):
        # default/legacy behaviour: no server token -> writes are not gated.
        wid = "noauthTestId01"
        with _stub_process_one(_FakeResult(ok=True, stage="mix", title=wid)):
            srv, port, qpath, worker_t, server_t = _start_server()
            threads = (worker_t, server_t)
            try:
                status, body = _post(port, [wid], [wid])
                self.assertEqual(status, 200)
                self.assertEqual(body["newly_added"], 1)
                events = _wait_until(port, lambda evs: wid in _completed_ids(evs))
                recs = [r for r in _completed_records(events) if r["watch_id"] == wid]
                self.assertEqual(recs[0]["status"], "done")
            finally:
                _cleanup(srv, qpath, threads)

    def test_post_add_rejected_without_token_when_configured(self):
        srv, port, qpath, worker_t, server_t = _start_server(token=self.TOKEN)
        threads = (worker_t, server_t)
        try:
            wid = "aBcDeFg9012"
            # no Authorization header -> 401, nothing queued and item never runs.
            status, body = _post(port, [wid], [wid])
            self.assertEqual(status, 401)
            self.assertEqual(body.get("error"), "authorization required")

            # wrong bearer -> still rejected (exact match).
            status, _ = _post(port, [wid], [], auth="Bearer totally-wrong")
            self.assertEqual(status, 401)
        finally:
            _cleanup(srv, qpath, threads)

    def test_post_add_accepted_with_valid_bearer_then_completes(self):
        # scheme is case-insensitive; a valid token both accepts the write and lets the worker run it.
        wid = "zYxWvUt9012"
        with _stub_process_one(_FakeResult(ok=True, stage="mix", title=wid)):
            srv, port, qpath, worker_t, server_t = _start_server(token=self.TOKEN)
            threads = (worker_t, server_t)
            try:
                status, body = _post(port, [wid], [wid], auth="Bearer %s" % self.TOKEN)
                self.assertEqual(status, 200)
                self.assertEqual(body["newly_added"], 1)
                events = _wait_until(port, lambda evs: wid in _completed_ids(evs))
                recs = [r for r in _completed_records(events) if r["watch_id"] == wid]
                self.assertEqual(len(recs), 1)
                self.assertEqual(recs[0]["status"], "done")
            finally:
                _cleanup(srv, qpath, threads)

    def test_scheme_is_case_insensitive(self):
        srv, port, qpath, worker_t, server_t = _start_server(token=self.TOKEN)
        threads = (worker_t, server_t)
        try:
            # uppercase Bearer + surrounding whitespace all accepted.
            status, body = _post(port, ["w01"], [], auth="  BEARER %s  " % self.TOKEN)
            self.assertEqual(status, 200)
            self.assertEqual(body["newly_added"], 1)
        finally:
            _cleanup(srv, qpath, threads)


class ServerArgParseTests(unittest.TestCase):
    """The server must expose the same flags as the CLI, so shared process_one code never crashes
    on an unexpected attribute. --replace-existing in particular is read unconditionally by
    run.process_one's finally; a missing attr there would crash every item on the server."""

    def test_server_parser_carries_replace_existing_by_default(self):
        args = build_parser().parse_args([])
        self.assertFalse(args.replace_existing)   # parity with the CLI default (wipe after each item)

    def test_server_parser_honors_replace_existing_flag(self):
        args = build_parser().parse_args(["--replace-existing"])
        self.assertTrue(args.replace_existing)

    def test_server_parser_carries_threads_and_output_dir(self):
        # process_one reads args.threads and args.output_dir unconditionally; the server parser must
        # define both (or every server job crashes with AttributeError). Defaults mirror the CLI.
        args = build_parser().parse_args([])
        self.assertEqual(args.threads, 0)
        self.assertEqual(args.output_dir, "output")
        self.assertEqual(args.audio_format, "mp3")

    def test_server_parser_honors_threads_and_output_dir(self):
        args = build_parser().parse_args(["-t", "4", "-o", "custom-out"])
        self.assertEqual(args.threads, 4)
        self.assertEqual(args.output_dir, "custom-out")


if __name__ == "__main__":
    unittest.main(verbosity=2)
