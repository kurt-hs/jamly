"""HTTP status server for splitfy (#16).

Shares the persistent WatchQueue protocol with run.py. A background worker runs one
queued item at a time via run.process_one, calling mark_done + save() after each item so
progress is resumable and can be streamed over SSE:

    python server.py --port 8765 [--scope .] [--queue splitfy_queue.json]

Endpoints:
    GET /status   SSE stream of {pending, done, current, progress} (text/event-stream)
    POST /add     seed inputs; returns newly queued ids (dedup by exact id)
    GET  /items   current pending list as JSON
    GET  /healthz health probe

Stdlib only. Heavy steps are delegated to run.process_one (yt-dlp + ffmpeg/GPU required on
PATH/scope); the HTTP layer itself is exercised in tests with a fake process_one."""
from __future__ import annotations   # noqa: F401  (makes all annotations lazy strings; needed so
                                     # ``X | None`` in instance-attr annotations works on 3.9+,
                                     # matching pyproject's requires-python = ">=3.9")
import argparse
import json
import os
import socket
import sys
import traceback
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from splitfy.watch_queue import WatchQueue, default_queue_path
from .core import AUDIO_FORMATS, DEFAULT_OUTPUT_FORMAT
from .run import ItemResult, process_one

# hosts that keep the queue reachable only by local processes; binding anywhere else is an explicit
# opt-in (and, with S2 hardening, additionally requires a server token). Empty host -> loopback.
def _is_loopback_host(host: str) -> bool:
    if not host:
        return True
    h = host.strip().lower()
    return h in ("127.0.0.1", "localhost", "::1")


def _authorized(header: str, token: str) -> bool:
    """True when `header` carries a Bearer token matching `token` (scheme case-insensitive)."""
    if not header or not token:
        return False
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return parts[1].strip() == token


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="splitfy HTTP status server (shares the queue protocol)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--scope", default=None, help="Working scope for engine/ytdlp/ffmpeg resolution (default: cwd)")
    p.add_argument("--queue", default=None, help="Queue file path (default: env AUTOCV_QUEUE or <scope>/splitfy_queue.json)")
    p.add_argument("--interval-ms", type=int, default=1000, help="SSE progress event interval")
    p.add_argument("--audio-format", choices=AUDIO_FORMATS, default=DEFAULT_OUTPUT_FORMAT,
                   help=("Final audio format; 'wav' = lossless/raw WAVs, '%s' = reasonably-encoded lossy (default)"
                         % DEFAULT_OUTPUT_FORMAT))
    p.add_argument("--idle-wait-s", type=float, default=2.0, help="Block this long when the queue is empty")
    # write-auth secret for POST /add: shared between this server and any client that enqueues
    # with it (also settable via env SPLITFY_SERVER_TOKEN). With no token configured, writes are
    # accepted as before; once one is set, every /add must carry a matching Authorization header.
    p.add_argument("--server-token", default=None,
                   help="Shared secret that gates POST /add (env: SPLITFY_SERVER_TOKEN); "
                        "required to bind off-loopback")
    return p


class _SilentServer(ThreadingHTTPServer):
    """Threaded HTTP server that quietly swallows expected disconnect errors.

    SSE streams are torn down constantly (client polls, popup closes); the default handler
    prints a traceback for every BrokenPipe/ConnectionReset, which is noisy and races with
    interpreter shutdown. We close the socket and ignore those, re-raising anything unexpected."""
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        if isinstance(request, socket.socket):
            try:
                request.close()
            except OSError:
                pass
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return   # expected during SSE teardown; stay quiet
        traceback.print_exc()  # noqa: E402  (unexpected handler error)


class ItemServer:
    """Owns a WatchQueue + worker thread; streams status to HTTP clients."""

    def __init__(self, args) -> None:
        self.args = args
        scope = os.environ.get("AUTOCV_SCOPE") or (args.scope or os.getcwd())
        qpath = getattr(args, "queue", None) or os.environ.get("AUTOCV_QUEUE") \
            or default_queue_path(scope)
        self.queue_path = os.path.abspath(qpath)
        self.queue = WatchQueue.from_path(self.queue_path)

        # status shared with the HTTP layer; guarded by _lock
        self._status_lock = threading.Lock()
        self._status = {
            "pending": list(self.queue.pending_ids()),
            "done": list(self.queue.done_ids()),
            "current": None,   # {"watch_id","stage","status","title","error"} or null
            "progress": {"total": len(self.queue), "pending": 0, "done": 0},
            # terminal record per finished item (done/error); stream so observers reliably see
            # each completion regardless of when they connect (single-slot ``current`` only ever
            # shows the last in-flight item).
            "completed": [],
        }

        # write-auth secret shared between this server and any client that enqueues with it.
        # env is authoritative (it crosses process boundaries); --server-token overrides for a
        # single instance. Empty -> no auth required (legacy behaviour).
        # --server-token is an explicit opt-in and overrides the environment; env is the fallback.
        self._token = getattr(args, "server_token", None) or os.environ.get("SPLITFY_SERVER_TOKEN")

        self._stop = threading.Event()
        # True only while start() has launched serve_forever(); stop() uses it to avoid blocking
        # on a server that was bound in __init__ but never served (BaseServer.shutdown would then
        # wait forever for the poller's shutdown event, which is only set by a running poller).
        self._serving: bool = False
        self._server: _SilentServer | None = None
        self._worker: threading.Thread | None = None
        # S2 hardening: refuse to bind anywhere reachable beyond the local host unless a token is
        # configured. Without one, an unauthenticated client could add items over the network.
        if not _is_loopback_host(self.args.host) and not self._token:
            raise ValueError(
                "binding %r:%d off-loopback requires --server-token (env: SPLITFY_SERVER_TOKEN); "
                "set it to allow non-loopback binds" % (self.args.host,
                                                        max(0, getattr(self.args, "port", 0))))
        # bind the HTTP server eagerly (deterministic port for tests; resumable status)
        self._server = _SilentServer((self.args.host, max(0, getattr(self.args, "port", 0))),
                                     QueueStatusHandler)
        # socketserver sets handler.server to this instance; expose our ItemServer to handlers
        self._server._item_server = self

    # -- worker ------------------------------------------------------------- #
    def _process_one(self) -> bool:
        """Process exactly one pending item, keeping the in-memory queue + status synced.

        Operates on the single persistent ``self.queue`` (the same instance /items and /status
        read), so counts never drift. Crash-resume is provided by __init__ loading from disk; a
        completed or errored item is moved to _done so it never re-runs on restart, while an item
        for which process_one raised is left pending (retry next cycle)."""
        # robust against an empty queue: remaining_work() is True when there are no items, but then
        # there is nothing to pick (a bare `next(...)` would raise StopIteration). Guard explicitly.
        pending_wids = [i["watch_id"] for i in self.queue.items if i["watch_id"] not in self.queue.done_ids()]
        if not pending_wids:
            return False   # empty queue or everything already done -> the idle loop just waits
        wid = pending_wids[0]
        with self._status_lock:
            cur = {"watch_id": wid, "stage": None, "status": "pending", "title": None, "error": None}
            self._status["current"] = dict(cur)

        # run the (possibly heavy) inference OUTSIDE any lock; the real worker updates progress
        # under short internal locks. If it raises here we leave the item pending for a retry.
        res = process_one(wid, self.args)
        if not res.ok:
            with self._status_lock:
                self._status["current"] = {"watch_id": wid, "stage": res.stage or "",
                                           "status": "error", "title": res.title, "error": res.error}
        else:
            # terminal 'done' state so clients/tests can see the finished item before the next cycle
            with self._status_lock:
                self._status["current"] = {"watch_id": wid, "stage": res.stage or "",
                                           "status": "done", "title": res.title, "error": None}
        # record this item's terminal state so it is observable even after ``current`` moves on
        with self._status_lock:
            rec = {"watch_id": wid, "stage": res.stage or "",
                   "status": "done" if res.ok else "error", "title": res.title,
                   "error": None if res.ok else res.error}
            self._status["completed"].append(rec)

        # success OR error -> record completion so it never re-runs on restart (avoids an infinite
        # loop hammering a persistent failure). Only PENDING items are moved to _done.
        try:
            self.queue.mark_done(wid)
        except Exception as exc:  # noqa: BLE001 - keep going; the item stays pending and retries
            with self._status_lock:
                self._status["current"]["error"] = "queue-mark-error: %s" % exc

        with self._status_lock:
            self._status["pending"] = list(self.queue.pending_ids())
            self._status["done"] = list(self.queue.done_ids())
        return True

    # Minimum interval between worker iterations even when ``--idle-wait-s`` is 0. A tight
    # busy-spin (sleep(0)) starves the HTTP handler thread so /status SSE can stall, and it also
    # leaves no visibility window for observers to read per-item completion between fast cycles.
    _MIN_IDLE_SLEEP_S = 0.05

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._process_one()
            except Exception as exc:  # noqa: BLE001 - never let the worker die silently
                with self._status_lock:
                    self._status["current"] = {"watch_id": None, "stage": "", "status": "error",
                                               "title": None, "error": str(exc)}
            # floor at _MIN_IDLE_SLEEP_S so the loop always yields to other threads (HTTP handler)
            time.sleep(min(self.args.idle_wait_s, self._MIN_IDLE_SLEEP_S))

    def start(self) -> None:
        """Launch the worker thread and serve HTTP. Binding happens in __init__."""
        self._serving = True
        self._worker = threading.Thread(target=self._loop, name="autoconvert-worker", daemon=True)
        self._worker.start()
        self._server.serve_forever()

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        server = self._server
        if server is not None:
            # Only wait for the poller when we actually started serving; otherwise just release
            # the socket (a never-started server's shutdown would block forever on __is_shut_down).
            if getattr(server, "_serving", False):
                server.shutdown()
            server.server_close()
            self._server.server_close()


class QueueStatusHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # enables keep-alive + chunked SSE streaming

    def log_message(self, fmt, *args):  # quiet by default; uncomment to debug
        pass

    @property
    def _server_obj(self) -> ItemServer:
        # self.server is the ThreadingHTTPServer instance (socketserver sets it);
        # we attach our ItemServer to it in start() so handlers can read shared state.
        return getattr(getattr(self, "server", None), "_item_server", None)

    # Cross-origin support for the browser clients: they run in an extension
    # (chrome-extension://) or a userscript (https://youtube.com/...) context and
    # fetch this http(s)://127.0.0.1 server, so without these headers the browsers
    # would block the response body on CORS grounds.
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):  # CORS preflight -> allow the cross-origin GET/POST above
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (http.server API)
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/status":
            s = self._server_obj
            # send SSE headers once, before streaming body lines
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while not s._stop.is_set():
                    with s._status_lock:
                        snap = json.loads(json.dumps(s._status))   # deep copy under lock
                        pr = snap["progress"]
                        total = pr["total"] or 1
                        done = len(snap["done"])
                        cur = snap.get("current") or {"watch_id": None, "stage": "", "status": "idle",
                                                       "title": None, "error": None}
                    line = json.dumps({"type": "progress", "pending": len(snap["pending"]),
                                       "done": done, "total": total, "current": cur,
                                       "last_completed": list(snap.get("completed") or [])})
                    self.wfile.write(("data: %s\n\n" % line).encode("utf-8"))
                    self.wfile.write((": tick p=%d/%d d=%d\n\n" % (pr["pending"], total, done)).encode("utf-8"))
                    self.wfile.flush()   # a write error here means the client disconnected
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True   # stop the keep-alive loop on a dead socket
                return  # client disconnected; drop this SSE connection
        if path == "/items":
            s = self._server_obj
            with s._status_lock:
                self._send_json({"pending": list(s.queue.pending_ids()),
                                 "done": list(s.queue.done_ids())})
            return
        if path == "/healthz":
            s = self._server_obj
            self._send_json({"ok": True, "queue_file": s.queue_path}, 200)
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path != "/add":
            self._send_json({"error": "unknown endpoint"}, status=404)
            return
        # read the request body up front so a rejected write never leaves unread bytes on a keep-alive socket
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        # S2 hardening: when a server token is configured, reject writes without it before touching the queue.
        s = self._server_obj
        if s._token and not _authorized(self.headers.get("Authorization", ""), s._token):
            body = json.dumps({"error": "authorization required"}).encode("utf-8")
            self.send_response(401)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("WWW-Authenticate", 'Bearer realm="splitfy-server"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)   # body must be written to match Content-Length
            return
        try:
            payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            inputs = [str(i).strip() for i in (payload.get("inputs") or [])]
            titles = payload.get("titles")
        except (ValueError, TypeError):
            self._send_json({"error": "invalid JSON body"}, status=400)
            return
        if not inputs:
            self._send_json({"queued": [], "note": "provide a non-empty 'inputs' list"}, status=400)
            return
        s = self._server_obj
        with s._status_lock:
            # add while holding the lock so the response counts are stable even under idle=0 churn;
            # items may be marked done by the worker only after this handler releases the lock.
            newly = s.queue.add_all(inputs, titles)
            pending_count = len(s.queue.pending_ids())
            s._status["pending"] = list(s.queue.pending_ids())
            s._status["done"] = list(s.queue.done_ids())
        # persist immediately so the worker sees them right away (resumable state)
        try:
            s.queue.save()
        except OSError as exc:
            self._send_json({"queued": inputs, "newly_added": newly, "pending_count": pending_count,
                             "warn": "save-error: %s" % exc}, status=207)
            return
        self._send_json({"queued": inputs, "newly_added": newly, "pending_count": pending_count})


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    srv = ItemServer(args)
    if not _is_loopback_host(args.host):
        # loud opt-in: anyone who can reach this host can read queue status; only writes are gated
        # by the token, so make the exposure unmistakable.
        print("WARNING: bound off-loopback (host=%s); the server is reachable over the network. "
              "Set SPLITFY_SERVER_TOKEN to require auth on POST /add." % args.host,
              file=sys.stderr)
    print("splitfy server: listening on http://%s:%d  (queue=%s)" % (args.host, args.port, srv.queue_path))
    try:
        srv.start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
