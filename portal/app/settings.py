"""Конфигурация портала из переменных окружения.

Портал намеренно маленький: он умеет только LDAP-вход и выдачу ключа.
Всё остальное (ключи, лимиты, команды, аналитика) живёт в LiteLLM.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet


class ConfigError(RuntimeError):
    """Неверная конфигурация портала."""


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name).lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name}: ожидалось true/false, получено {value!r}")


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидалось целое число, получено {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name}: значение должно быть >= {minimum}")
    return value


@dataclass(frozen=True)
class LdapSettings:
    uri: str
    start_tls: bool = False
    tls_verify: bool = True
    bind_dn: str = ""
    bind_password: str = ""
    user_base: str = ""
    user_filter: str = "(&(objectClass=person)(sAMAccountName={username}))"
    display_name_attr: str = "displayName"
    email_attr: str = "mail"
    group_attr: str = "memberOf"
    group_base: str = ""
    group_filter: str = "(&(objectClass=group)(member={dn}))"
    nested_groups: bool = False
    timeout: int = 8

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.uri:
            problems.append("LDAP_URI не задан (например ldap://dc01.corp.local:389)")
        elif not self.uri.startswith(("ldap://", "ldaps://")):
            problems.append("LDAP_URI должен начинаться с ldap:// или ldaps://")
        if not self.user_base:
            problems.append("LDAP_USER_BASE не задан (например OU=Users,DC=corp,DC=local)")
        if "{username}" not in self.user_filter:
            problems.append("LDAP_USER_FILTER должен содержать {username}")
        if not self.bind_dn:
            problems.append("LDAP_BIND_DN не задан: нужен сервисный аккаунт")
        if not self.bind_password:
            problems.append("LDAP_BIND_PASSWORD не задан")
        if not 1 <= self.timeout <= 60:
            problems.append("LDAP_TIMEOUT должен быть в диапазоне 1..60 секунд")
        return problems


@dataclass(frozen=True)
class LoginPolicy:
    max_attempts: int = 5
    window_minutes: int = 10
    lockout_minutes: int = 15


@dataclass(frozen=True)
class Settings:
    env: str
    policy_path: Path
    catalog_path: Path
    litellm_base_url: str
    litellm_master_key: str
    litellm_timeout: int
    session_key: str
    session_ttl_hours: int
    cookie_name: str
    cookie_secure: bool
    ldap: LdapSettings
    login: LoginPolicy
    timezone: str
    public_api_base_url: str
    app_name: str = "LLMGate"
    trusted_proxies: tuple[str, ...] = field(default_factory=tuple)

    def validate(self) -> None:
        problems: list[str] = []
        if not self.litellm_master_key.startswith("sk-"):
            problems.append("LITELLM_MASTER_KEY должен начинаться с sk-")
        if len(self.litellm_master_key) < 16:
            problems.append("LITELLM_MASTER_KEY слишком короткий (минимум 16 символов)")
        try:
            Fernet(self.session_key.encode())
        except Exception:  # noqa: BLE001
            problems.append("PORTAL_SESSION_KEY должен быть 32-байтным urlsafe-base64 ключом Fernet")
        if not 1 <= self.session_ttl_hours <= 168:
            problems.append("PORTAL_SESSION_TTL_HOURS должен быть в диапазоне 1..168")
        if not self.litellm_base_url.startswith(("http://", "https://")):
            problems.append("LITELLM_BASE_URL должен начинаться с http:// или https://")
        problems.extend(self.ldap.validate())
        if problems:
            raise ConfigError("Проблемы конфигурации:\n  - " + "\n  - ".join(problems))

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            env=_env("PORTAL_ENV", "production"),
            policy_path=Path(_env("PORTAL_POLICY_PATH", "/app/config/access.yaml")),
            catalog_path=Path(_env("PORTAL_CATALOG_PATH", "/app/config/models.generated.json")),
            litellm_base_url=_env("LITELLM_BASE_URL", "http://litellm:4000").rstrip("/"),
            litellm_master_key=_env("LITELLM_MASTER_KEY"),
            litellm_timeout=_env_int("LITELLM_TIMEOUT", 20, minimum=1),
            session_key=_env("PORTAL_SESSION_KEY"),
            session_ttl_hours=_env_int("PORTAL_SESSION_TTL_HOURS", 8, minimum=1),
            cookie_name=_env("PORTAL_COOKIE_NAME", "llmgate_session"),
            cookie_secure=_env_bool("PORTAL_COOKIE_SECURE", False),
            ldap=LdapSettings(
                uri=_env("LDAP_URI"),
                start_tls=_env_bool("LDAP_START_TLS", False),
                tls_verify=_env_bool("LDAP_TLS_VERIFY", True),
                bind_dn=_env("LDAP_BIND_DN"),
                bind_password=os.environ.get("LDAP_BIND_PASSWORD", ""),
                user_base=_env("LDAP_USER_BASE"),
                user_filter=_env("LDAP_USER_FILTER", "(&(objectClass=person)(sAMAccountName={username}))"),
                display_name_attr=_env("LDAP_DISPLAY_NAME_ATTR", "displayName"),
                email_attr=_env("LDAP_EMAIL_ATTR", "mail"),
                group_attr=_env("LDAP_GROUP_ATTR", "memberOf"),
                group_base=_env("LDAP_GROUP_BASE"),
                group_filter=_env("LDAP_GROUP_FILTER", "(&(objectClass=group)(member={dn}))"),
                nested_groups=_env_bool("LDAP_NESTED_GROUPS", False),
                timeout=_env_int("LDAP_TIMEOUT", 8, minimum=1),
            ),
            login=LoginPolicy(
                max_attempts=_env_int("PORTAL_LOGIN_MAX_ATTEMPTS", 5, minimum=1),
                window_minutes=_env_int("PORTAL_LOGIN_WINDOW_MINUTES", 10, minimum=1),
                lockout_minutes=_env_int("PORTAL_LOGIN_LOCKOUT_MINUTES", 15, minimum=1),
            ),
            timezone=_env("TZ", "Europe/Moscow"),
            public_api_base_url=_env("PUBLIC_API_BASE_URL", "http://localhost:4000/v1"),
            trusted_proxies=tuple(p.strip() for p in _env("PORTAL_TRUSTED_PROXIES").split(",") if p.strip()),
        )
        settings.validate()
        return settings
