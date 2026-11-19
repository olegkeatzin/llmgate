"""Подписанная cookie сессии, CSRF и защитные заголовки.

Сессия живёт в зашифрованной cookie: портал не хранит серверного
состояния. Отозвать все сессии разом можно сменой PORTAL_SESSION_KEY.
"""

from __future__ import annotations

import json
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

MAX_COOKIE_BYTES = 3800


class SessionCodec:
    def __init__(self, key: str, ttl_hours: int) -> None:
        self._fernet = Fernet(key.encode())
        self.ttl_seconds = max(1, ttl_hours) * 3600

    def encode(
        self,
        *,
        username: str,
        display_name: str,
        groups: list[str],
        csrf: str,
    ) -> str:
        payload = {
            "u": username,
            "n": display_name[:120],
            "g": [g[:150] for g in groups[:40]],
            "c": csrf,
            "e": int(time.time()) + self.ttl_seconds,
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        return self._fernet.encrypt(raw).decode()

    def decode(self, token: str) -> dict | None:
        if not token or len(token) > MAX_COOKIE_BYTES:
            return None
        try:
            raw = self._fernet.decrypt(token.encode(), ttl=None)
            payload = json.loads(raw)
        except (InvalidToken, ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            if int(payload.get("e", 0)) <= int(time.time()):
                return None
            username = str(payload.get("u") or "")
            if not username:
                return None
        except (TypeError, ValueError):
            return None
        return payload


class LoginThrottle:
    """Счётчик неудачных входов в памяти процесса.

    Портал запускается одним воркером, поэтому распределённое состояние
    здесь не нужно. После рестарта счётчики сбрасываются — это осознанный
    компромисс ради отсутствия БД.
    """

    def __init__(self, max_attempts: int, window_seconds: int, lockout_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window = window_seconds
        self.lockout = lockout_seconds
        self._state: dict[str, dict] = {}
        self._limit = 10_000

    def _key(self, username: str, ip: str) -> str:
        return f"{username.lower()}|{ip}"

    def locked(self, username: str, ip: str) -> tuple[bool, int]:
        entry = self._state.get(self._key(username, ip))
        if not entry:
            return False, 0
        now = time.monotonic()
        until = entry.get("until", 0)
        if until > now:
            return True, int(until - now)
        return False, 0

    def fail(self, username: str, ip: str) -> None:
        if len(self._state) > self._limit:
            self._state.clear()
        key = self._key(username, ip)
        now = time.monotonic()
        entry = self._state.setdefault(key, {"fails": [], "until": 0})
        entry["fails"] = [t for t in entry["fails"] if now - t < self.window]
        entry["fails"].append(now)
        if len(entry["fails"]) >= self.max_attempts:
            entry["until"] = now + self.lockout
            entry["fails"] = []

    def success(self, username: str, ip: str) -> None:
        self._state.pop(self._key(username, ip), None)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def constant_time_eq(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode(), right.encode())


def client_ip(request: Request, trusted: tuple[str, ...]) -> str:
    peer = request.client.host if request.client else ""
    if trusted and peer in trusted:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return peer[:64]


def set_session_cookie(response: Response, *, name: str, token: str, max_age: int, secure: bool) -> None:
    response.set_cookie(name, token, max_age=max_age, httponly=True, secure=secure, samesite="lax", path="/")


def clear_session_cookie(response: Response, *, name: str, secure: bool) -> None:
    response.delete_cookie(name, path="/", httponly=True, secure=secure, samesite="lax")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, hsts: bool = False) -> None:  # noqa: ANN001
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        response.headers.setdefault("Cache-Control", "no-store")
        if self.hsts:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response
