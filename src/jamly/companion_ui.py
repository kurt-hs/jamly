"""Loopback UI assets and short-lived, file-delivered browser sessions."""
from __future__ import annotations

import hmac
import html
import json
import os
from pathlib import Path
import secrets
import threading
import time
import warnings

from .runtime import browser_assets

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
LAUNCH_LIFETIME = 120
SESSION_LIFETIME = 24 * 60 * 60


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        warnings.warn("Cannot remove expired companion launch file %s: %s" % (path, exc))


class CompanionUI:
    def __init__(self, state: Path, enabled: bool):
        self.state = state
        self.enabled = enabled
        self._lock = threading.Lock()
        self._launches: dict[str, tuple[float, Path]] = {}
        self._sessions: dict[str, float] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        for ticket, (expires, path) in list(self._launches.items()):
            if now >= expires:
                _remove(path)
                del self._launches[ticket]
        self._sessions = {key: expiry for key, expiry in self._sessions.items() if expiry > now}

    def authorize(self, header: str) -> bool:
        parts = header.split(None, 1)
        if not self.enabled or len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        with self._lock:
            self._prune()
            return any(hmac.compare_digest(parts[1].encode(), token.encode())
                       for token in self._sessions)

    def launch(self, base_url: str) -> Path:
        with self._lock:
            if not self.enabled:
                raise ValueError("The companion interface is available only on a running loopback backend")
            self._prune()
            if len(self._launches) >= 16:
                raise ValueError("Too many pending browser launches; wait two minutes and try again")
            directory = self.state / "browser-sessions"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            # A crashed process cannot accept its old tickets. Remove only our expired launch files.
            for old in directory.glob("launch-*.html"):
                if old.is_file() and time.time() - old.stat().st_mtime > LAUNCH_LIFETIME:
                    _remove(old)
            ticket = secrets.token_urlsafe(32)
            nonce = secrets.token_urlsafe(24)
            path = directory / ("launch-" + secrets.token_hex(16) + ".html")
            policy = "default-src 'none'; script-src 'nonce-%s'; form-action %s; base-uri 'none'" % (
                nonce, base_url,
            )
            content = (
                '<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="referrer" content="no-referrer">'
                '<meta http-equiv="Content-Security-Policy" content="%s">'
                '<title>Opening Jamly</title></head><body><p>Opening your local Jamly companion...</p>'
                '<form method="post" action="%s/app/session">'
                '<input type="hidden" name="ticket" value="%s"><button>Open Jamly</button></form>'
                '<script nonce="%s">document.forms[0].submit();</script></body></html>'
            ) % (html.escape(policy, quote=True), html.escape(base_url, quote=True), ticket, nonce)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(content)
            self._launches[ticket] = (time.monotonic() + LAUNCH_LIFETIME, path)
            return path

    def session_page(self, ticket: str) -> tuple[bytes, str]:
        with self._lock:
            if not self.enabled:
                raise ValueError("Companion sessions require a running loopback backend")
            self._prune()
            launch = self._launches.pop(ticket, None)
            if launch is None:
                raise ValueError("This launch has expired or was already used. Open Jamly again.")
            _remove(launch[1])
            if len(self._sessions) >= 32:
                oldest = min(self._sessions, key=self._sessions.get)
                del self._sessions[oldest]
            token = secrets.token_urlsafe(32)
            self._sessions[token] = time.monotonic() + SESSION_LIFETIME
        nonce = secrets.token_urlsafe(24)
        body = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<title>Jamly</title></head><body><script nonce="%s">'
            'sessionStorage.setItem("jamly-token", %s);location.replace("/app/");'
            '</script></body></html>'
        ) % (nonce, json.dumps(token))
        return body.encode(), CSP.replace("script-src 'self'", "script-src 'nonce-%s'" % nonce)

    def asset(self, path: str) -> tuple[bytes, str]:
        if not self.enabled:
            raise FileNotFoundError("Companion UI requires a loopback backend")
        web = Path(__file__).parent / "web"
        assets = {
            "/app/": (web / "index.html", "text/html; charset=utf-8"),
            "/app/app.js": (web / "app.js", "text/javascript; charset=utf-8"),
            "/app/app.css": (web / "app.css", "text/css; charset=utf-8"),
            "/app/jamly_client.js": (browser_assets("extension") / "jamly_client.js",
                                   "text/javascript; charset=utf-8"),
        }
        if path not in assets:
            raise FileNotFoundError("Unknown companion asset")
        file, content_type = assets[path]
        return file.read_bytes(), content_type

    def close(self) -> None:
        with self._lock:
            for _expiry, path in self._launches.values():
                _remove(path)
            self._launches.clear()
            self._sessions.clear()
            self.enabled = False
