# -*- coding: utf-8 -*-
"""api_client.py — DatXeVui admin-API client with auth, pacing and retries.

Handles the three gates the backend puts in front of /api/*:
  1. anti-scraping UA filter      → browser User-Agent
  2. mutation Origin check        → X-Client: mobile + Origin header
     (the native-mobile path is exempt from the UA + origin checks)
  3. token-bucket rate limit      → client-side pacing + 429 backoff
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")


class ApiError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} on {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


class Client:
    def __init__(self, base_url: str, email: str, password: str,
                 rps: float = 8.0, timeout: int = 60, log=print):
        self.base = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.rps = max(0.5, float(rps))
        self.timeout = timeout
        self.log = log
        self.token = None
        self.token_at = 0.0
        self._min_interval = 1.0 / self.rps
        self._last_req = [0.0]
        self._lock = threading.Lock()
        self._requests = 0

    # ── auth ────────────────────────────────────────────────────────
    def login(self) -> None:
        body = json.dumps({"email": self.email, "password": self.password})
        req = urllib.request.Request(
            f"{self.base}/api/auth/login", data=body.encode(),
            method="POST",
            headers=self._base_headers({"Content-Type": "application/json"}))
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read())
        tok = (data.get("tokens") or {}).get("access_token")
        if not tok:
            raise RuntimeError(f"login returned no token: {data}")
        self.token = tok
        self.token_at = time.time()
        self.log("[auth] logged in")

    def _ensure_token(self) -> None:
        if not self.token or time.time() - self.token_at > 25 * 60:
            self.login()

    # ── headers ─────────────────────────────────────────────────────
    def _base_headers(self, extra: dict | None = None) -> dict:
        h = {
            "User-Agent": BROWSER_UA,
            "X-Client": "mobile",
            "Origin": urllib.parse.urlsplit(self.base)
            ._replace(path="", query="", fragment="").geturl(),
        }
        if extra:
            h.update(extra)
        return h

    # ── pacing ──────────────────────────────────────────────────────
    def _pace(self) -> None:
        with self._lock:
            wait = self._min_interval - (time.time() - self._last_req[0])
            if wait > 0:
                time.sleep(wait)
            self._last_req[0] = time.time()

    # ── core request ────────────────────────────────────────────────
    def request(self, method: str, path: str, body: dict | None = None,
                raw: bytes | None = None, content_type: str | None = None,
                retries: int = 5) -> bytes:
        """Authenticated JSON (or raw) request with retry + re-login."""
        url = path if path.startswith("http") else f"{self.base}{path}"
        # Auth endpoints must run without a token — on a fresh DB there is
        # no user to log in as yet (bootstrap registers the first admin).
        skip_auth = path.startswith(("/api/auth/login", "/api/auth/register"))
        last_err = None
        for attempt in range(retries + 1):
            if not skip_auth:
                self._ensure_token()
            self._pace()
            headers = self._base_headers()
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            if content_type:
                headers["Content-Type"] = content_type
            data = None
            if raw is not None:
                data = raw
            elif body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, method=method,
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    self._requests += 1
                    return r.read()
            except urllib.error.HTTPError as e:
                payload = e.read().decode("utf-8", "replace")
                self._requests += 1
                if e.code == 401 and attempt < retries:
                    self.token = None
                    last_err = ApiError(401, payload, url)
                    continue
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    last_err = ApiError(e.code, payload, url)
                    time.sleep(min(2 ** attempt * 0.5, 15.0))
                    continue
                raise ApiError(e.code, payload, url)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < retries:
                    last_err = e
                    time.sleep(min(2 ** attempt * 0.5, 15.0))
                    continue
                raise
        raise last_err or RuntimeError("request failed")

    def get_json(self, path: str):
        return json.loads(self.request("GET", path))

    def post_json(self, path: str, body: dict):
        return json.loads(self.request("POST", path, body=body))

    def put_json(self, path: str, body: dict):
        return json.loads(self.request("PUT", path, body=body))

    # ── multipart upload (route pictures) ────────────────────────────
    def upload_picture(self, route_id: str, file_path: str, alt: str | None):
        boundary = "----datxevui-migration-7d9f2a"
        with open(file_path, "rb") as f:
            blob = f.read()
        fname = file_path.rsplit("/", 1)[-1]
        mime = ("image/png" if fname.endswith(".png")
                else "image/webp" if fname.endswith(".webp")
                else "image/jpeg")
        parts = []
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{fname}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
            + blob + b"\r\n")
        if alt:
            alt_san = alt.replace('"', "'").replace("\r", " ").replace("\n", " ")
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; '
                f'name="alt_text"\r\n\r\n{alt_san}\r\n'.encode())
        parts.append(f"--{boundary}--\r\n".encode())
        payload = b"".join(parts)
        return json.loads(self.request(
            "POST", f"/api/admin/routes/{route_id}/pictures", raw=payload,
            content_type=f"multipart/form-data; boundary={boundary}"))
