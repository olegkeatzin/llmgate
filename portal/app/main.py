"""LLMGate — портал выдачи API-ключей.

Портал делает ровно одно: превращает вход через LDAP в виртуальный ключ
LiteLLM и показывает сотруднику его ключи. Ключи, лимиты, бюджеты,
аналитика и администрирование — в LiteLLM Proxy.
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .catalog import ModelCatalog
from .ldap_auth import InvalidCredentials, LdapAuthenticator, LdapError, LdapUnavailable
from .litellm_client import LiteLLMClient, LiteLLMError
from .policy import Policy, PolicySet, build_key_payload
from .security import (
    LoginThrottle,
    SecurityHeadersMiddleware,
    SessionCodec,
    clear_session_cookie,
    client_ip,
    constant_time_eq,
    new_csrf_token,
    set_session_cookie,
)
from .settings import Settings
from .ui import STATIC_DIR, build_templates

log = logging.getLogger(__name__)


class AuthRequired(Exception):
    """Нет действующей сессии."""


class PolicyDenied(Exception):
    """Учётная запись вошла, но роли в access.yaml нет."""


class CsrfFailure(Exception):
    """Не совпал CSRF-токен."""


@dataclass
class CurrentUser:
    username: str
    display_name: str
    groups: tuple[str, ...]
    csrf: str
    policy: Policy


@dataclass
class KeyView:
    token: str
    alias: str
    models: list[str]
    created_at: datetime | None
    expires_at: datetime | None
    spend: float
    status: str

    @property
    def is_active(self) -> bool:
        return self.status == "active"


# --------------------------------------------------------------------------- #
# Зависимости
# --------------------------------------------------------------------------- #


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_litellm(request: Request) -> LiteLLMClient:
    return request.app.state.litellm


def get_catalog(request: Request) -> ModelCatalog:
    return request.app.state.catalog


def _resolve_user(request: Request) -> CurrentUser:
    settings: Settings = request.app.state.settings
    payload = request.app.state.codec.decode(request.cookies.get(settings.cookie_name, ""))
    if payload is None:
        raise AuthRequired()
    policy = request.app.state.policies.resolve(payload["u"], payload.get("g", []))
    if policy is None:
        raise PolicyDenied()
    return CurrentUser(
        username=payload["u"],
        display_name=payload.get("n") or payload["u"],
        groups=tuple(payload.get("g", [])),
        csrf=payload.get("c", ""),
        policy=policy,
    )


async def current_user(request: Request) -> CurrentUser:
    return _resolve_user(request)


async def optional_user(request: Request) -> CurrentUser | None:
    try:
        return _resolve_user(request)
    except (AuthRequired, PolicyDenied):
        return None


def check_csrf(user: CurrentUser, submitted: str) -> None:
    if not submitted or not constant_time_eq(submitted, user.csrf):
        raise CsrfFailure()


def safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


# --------------------------------------------------------------------------- #
# Разбор ответов LiteLLM
# --------------------------------------------------------------------------- #


def _parse_dt(value: object) -> datetime | None:
    if value is None or value == "" or value == "never":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def to_key_views(raw_keys: list[dict]) -> list[KeyView]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    views: list[KeyView] = []
    for raw in raw_keys:
        token = str(raw.get("token") or "").strip()
        if not token:
            continue
        expires_at = _parse_dt(raw.get("expires"))
        blocked = bool(raw.get("blocked"))
        if blocked:
            status = "revoked"
        elif expires_at is not None and expires_at <= now:
            status = "expired"
        else:
            status = "active"
        models = raw.get("models") or []
        views.append(
            KeyView(
                token=token,
                alias=str(raw.get("key_alias") or raw.get("key_name") or "—"),
                models=[str(m) for m in models] if isinstance(models, list) else [],
                created_at=_parse_dt(raw.get("created_at")),
                expires_at=expires_at,
                spend=float(raw.get("spend") or 0.0),
                status=status,
            )
        )
    order = {"active": 0, "expired": 1, "revoked": 2}
    views.sort(key=lambda k: (order.get(k.status, 3), k.expires_at or datetime.max))
    return views


# --------------------------------------------------------------------------- #
# Маршруты
# --------------------------------------------------------------------------- #

router = APIRouter()


def _render(request: Request, name: str, context: dict, status_code: int = 200) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, name, context, status_code=status_code)


def _render_login(
    request: Request,
    *,
    error: str | None = None,
    next_url: str = "/",
    username: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    return _render(
        request,
        "login.html",
        {"error": error, "next": next_url, "username": username, "app_name": "LLMGate", "version": __version__},
        status_code=status_code,
    )


def _render_index(
    request: Request,
    user: CurrentUser,
    keys: list[KeyView],
    *,
    revealed: dict | None = None,
    error: str | None = None,
    keys_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    catalog: ModelCatalog = request.app.state.catalog
    models = catalog.for_role(list(user.policy.models))
    served = catalog.served_names(list(user.policy.models))
    active = [k for k in keys if k.is_active]
    return _render(
        request,
        "index.html",
        {
            "user": user,
            "policy": user.policy,
            "keys": keys,
            "active_count": len(active),
            "models": models,
            "served_models": served,
            "first_model": served[0] if served else "model",
            "api_base": settings.public_api_base_url,
            "revealed": revealed,
            "error": error,
            "keys_error": keys_error,
            "app_name": "LLMGate",
            "version": __version__,
        },
        status_code=status_code,
    )


async def _load_keys(litellm: LiteLLMClient, username: str) -> tuple[list[KeyView], str | None]:
    try:
        return to_key_views(await litellm.list_keys(username)), None
    except LiteLLMError as exc:
        return [], str(exc)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "", user: CurrentUser | None = Depends(optional_user)) -> HTMLResponse:
    target = safe_next(next)
    if user is not None:
        return RedirectResponse(target, status_code=303)  # type: ignore[return-value]
    return _render_login(request, next_url=target)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    target = safe_next(next)
    username = username.strip()[:128]
    ip = client_ip(request, settings.trusted_proxies)
    throttle: LoginThrottle = request.app.state.throttle

    locked, seconds_left = throttle.locked(username, ip)
    if locked:
        minutes = max(1, seconds_left // 60)
        return _render_login(
            request,
            error=f"Слишком много неудачных попыток. Повторите через {minutes} мин.",
            next_url=target,
            username=username,
            status_code=429,
        )

    if not username or not password:
        return _render_login(
            request, error="Введите логин и пароль.", next_url=target, username=username, status_code=400
        )

    try:
        identity = request.app.state.ldap.authenticate(username, password)
    except InvalidCredentials:
        throttle.fail(username, ip)
        log.warning("event=login.failed user=%r ip=%s", username, ip)
        return _render_login(
            request, error="Неверный логин или пароль.", next_url=target, username=username, status_code=401
        )
    except LdapUnavailable:
        log.error("event=login.ldap_unavailable user=%r ip=%s", username, ip)
        return _render_login(
            request,
            error="Сервер LDAP недоступен. Попробуйте позже или обратитесь к администратору.",
            next_url=target,
            username=username,
            status_code=503,
        )
    except LdapError:
        log.exception("event=login.ldap_error user=%r ip=%s", username, ip)
        return _render_login(
            request, error="Ошибка аутентификации. Обратитесь к администратору.", next_url=target, username=username, status_code=503
        )

    throttle.success(username, ip)

    policy = request.app.state.policies.resolve(identity.username, identity.groups)
    if policy is None:
        log.warning("event=login.denied user=%r groups=%s", identity.username, identity.groups[:10])
        return _render_login(
            request,
            error=(
                "У вашей учётной записи нет доступа к LLM-сервису. Обратитесь к администратору: "
                "нужно добавить вас в группу доступа."
            ),
            next_url=target,
            username=username,
            status_code=403,
        )

    token = request.app.state.codec.encode(
        username=identity.username,
        display_name=identity.display_name,
        groups=identity.groups,
        csrf=new_csrf_token(),
    )
    log.info("event=login.ok user=%r policy=%s ip=%s", identity.username, policy.id, ip)
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(
        response,
        name=settings.cookie_name,
        token=token,
        max_age=settings.session_ttl_hours * 3600,
        secure=settings.cookie_secure,
    )
    return response  # type: ignore[return-value]


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    settings: Settings = request.app.state.settings
    response = RedirectResponse("/login?msg=logout", status_code=303)
    clear_session_cookie(response, name=settings.cookie_name, secure=settings.cookie_secure)
    return response


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    user: CurrentUser = Depends(current_user),
    litellm: LiteLLMClient = Depends(get_litellm),
) -> HTMLResponse:
    keys, keys_error = await _load_keys(litellm, user.username)
    return _render_index(request, user, keys, keys_error=keys_error)


@router.post("/keys", response_class=HTMLResponse)
async def create_key(
    request: Request,
    user: CurrentUser = Depends(current_user),
    litellm: LiteLLMClient = Depends(get_litellm),
    catalog: ModelCatalog = Depends(get_catalog),
) -> HTMLResponse:
    form = await request.form()
    check_csrf(user, str(form.get("_csrf") or ""))

    served = catalog.served_names(list(user.policy.models))
    if not served:
        return _render_index(
            request,
            user,
            [],
            error="Для вашей роли не включено ни одной модели. Обратитесь к администратору.",
            status_code=403,
        )

    keys, keys_error = await _load_keys(litellm, user.username)
    if keys_error:
        return _render_index(request, user, keys, error=keys_error, status_code=502)

    active = [k for k in keys if k.is_active]
    if len(active) >= user.policy.max_active_keys:
        return _render_index(
            request,
            user,
            keys,
            error=(
                f"У вас уже {len(active)} действующих ключей (лимит {user.policy.max_active_keys}). "
                "Отзовите ненужный ключ, чтобы выпустить новый."
            ),
            status_code=409,
        )

    raw_alias = str(form.get("alias") or "").strip()
    alias = "".join(ch for ch in raw_alias if ch.isprintable())[:60]
    key_alias = alias or f"{user.username}-{secrets.token_hex(2)}"

    payload = build_key_payload(
        username=user.username,
        groups=user.groups,
        policy=user.policy,
        served_models=served,
        key_alias=key_alias,
    )

    try:
        data = await litellm.generate_key(payload)
    except LiteLLMError as exc:
        log.error("event=key.create_failed user=%r error=%s", user.username, exc)
        return _render_index(request, user, keys, error=f"Не удалось выпустить ключ: {exc}", status_code=502)

    key_value = str(data.get("key") or "")
    if not key_value:
        log.error("event=key.create_bad_response user=%r keys=%s", user.username, sorted(data))
        return _render_index(
            request, user, keys, error="LiteLLM вернул некорректный ответ при выпуске ключа.", status_code=502
        )

    expires_at = _parse_dt(data.get("expires")) or (
        datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=user.policy.key_ttl_hours)
    )
    log.info(
        "event=key.created user=%r alias=%r models=%s ttl_h=%s",
        user.username,
        key_alias,
        served,
        user.policy.key_ttl_hours,
    )

    refreshed, refresh_error = await _load_keys(litellm, user.username)
    return _render_index(
        request,
        user,
        refreshed or keys,
        revealed={"key": key_value, "alias": key_alias, "expires_at": expires_at},
        keys_error=refresh_error,
        status_code=201,
    )


@router.post("/keys/{token}/revoke", response_class=HTMLResponse)
async def revoke_key(
    token: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    litellm: LiteLLMClient = Depends(get_litellm),
) -> HTMLResponse:
    form = await request.form()
    check_csrf(user, str(form.get("_csrf") or ""))

    keys, keys_error = await _load_keys(litellm, user.username)
    if keys_error:
        return _render_index(request, user, keys, error=keys_error, status_code=502)
    if token not in {k.token for k in keys}:
        return _render_index(request, user, keys, error="Ключ не найден.", status_code=404)

    try:
        await litellm.delete_key(token)
    except LiteLLMError as exc:
        log.error("event=key.revoke_failed user=%r error=%s", user.username, exc)
        return _render_index(request, user, keys, error=f"Не удалось отозвать ключ: {exc}", status_code=502)

    log.info("event=key.revoked user=%r", user.username)
    return RedirectResponse("/?msg=revoked", status_code=303)  # type: ignore[return-value]


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(litellm: LiteLLMClient = Depends(get_litellm)) -> JSONResponse:
    healthy = await litellm.health()
    return JSONResponse({"ready": healthy, "litellm": healthy}, status_code=200 if healthy else 503)


# --------------------------------------------------------------------------- #
# Приложение
# --------------------------------------------------------------------------- #


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def create_app(
    settings: Settings | None = None,
    *,
    ldap: LdapAuthenticator | None = None,
    litellm: LiteLLMClient | None = None,
    catalog: ModelCatalog | None = None,
    policies: PolicySet | None = None,
    templates=None,  # noqa: ANN001
) -> FastAPI:
    _configure_logging()
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("LLMGate portal %s started (env=%s)", __version__, settings.env)
        yield
        await app.state.litellm.aclose()

    app = FastAPI(title="LLMGate", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.policies = policies or PolicySet.load(settings.policy_path)
    app.state.catalog = catalog or ModelCatalog.load(settings.catalog_path)
    app.state.ldap = ldap or LdapAuthenticator(settings.ldap)
    app.state.litellm = litellm or LiteLLMClient(
        settings.litellm_base_url, settings.litellm_master_key, timeout=float(settings.litellm_timeout)
    )
    app.state.codec = SessionCodec(settings.session_key, settings.session_ttl_hours)
    app.state.throttle = LoginThrottle(
        max_attempts=settings.login.max_attempts,
        window_seconds=settings.login.window_minutes * 60,
        lockout_seconds=settings.login.lockout_minutes * 60,
    )
    app.state.templates = templates or build_templates(settings.timezone)

    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.cookie_secure)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    _register_error_handlers(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    def error_page(request: Request, code: int, message: str) -> HTMLResponse:
        return request.app.state.templates.TemplateResponse(
            request,
            "error.html",
            {"code": code, "message": message, "app_name": "LLMGate", "version": __version__},
            status_code=code,
        )

    @app.exception_handler(AuthRequired)
    async def _auth_required(request: Request, exc: AuthRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        target = request.url.path
        if request.url.query:
            target = target + "?" + request.url.query
        return RedirectResponse("/login?next=" + quote(target, safe=""), status_code=303)

    @app.exception_handler(PolicyDenied)
    async def _policy_denied(request: Request, exc: PolicyDenied):
        return error_page(
            request,
            403,
            "Для вашей учётной записи не настроена роль в config/access.yaml. Обратитесь к администратору.",
        )

    @app.exception_handler(CsrfFailure)
    async def _csrf(request: Request, exc: CsrfFailure):
        return error_page(
            request, 403, "Форма устарела или защитный токен не совпал. Обновите страницу и повторите."
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith(("/api/", "/healthz", "/readyz")):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        message = {404: "Страница не найдена.", 405: "Метод не поддерживается."}.get(exc.status_code, "Запрос отклонён.")
        return error_page(request, exc.status_code, message)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("event=unhandled path=%s", request.url.path)
        return error_page(
            request, 500, "Внутренняя ошибка. Подробности — в журнале контейнера portal."
        )
