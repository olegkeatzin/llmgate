"""Интеграционные тесты портала с поддельными LDAP и LiteLLM.

Ни сети, ни Docker не требуется: оба внешних сервиса подменены,
шаблоны и статика — настоящие.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.catalog import ModelCatalog
from app.ldap_auth import InvalidCredentials, LdapIdentity
from app.main import create_app
from app.policy import PolicySet
from app.settings import LdapSettings, LoginPolicy, Settings
from app.ui import build_templates

ROOT = Path(__file__).resolve().parents[1]
CSRF_RE = re.compile(r'name="_csrf" value="([^"]+)"')


def make_settings(**overrides) -> Settings:
    values = dict(
        env="test",
        policy_path=ROOT / "config" / "access.yaml",
        catalog_path=ROOT / "config" / "models.generated.json",
        litellm_base_url="http://litellm:4000",
        litellm_master_key="sk-test-" + "a" * 24,
        litellm_timeout=5,
        session_key=Fernet.generate_key().decode(),
        session_ttl_hours=8,
        cookie_name="llmgate_session",
        cookie_secure=False,
        ldap=LdapSettings(
            uri="ldap://ldap.test",
            user_base="OU=Users,DC=corp,DC=local",
            bind_dn="CN=svc,DC=corp,DC=local",
            bind_password="secret",
        ),
        login=LoginPolicy(max_attempts=3, window_minutes=10, lockout_minutes=15),
        timezone="UTC",
        public_api_base_url="http://localhost:4000/v1",
    )
    values.update(overrides)
    settings = Settings(**values)
    settings.validate()
    return settings


class FakeLdap:
    users = {
        "ivanov": {"password": "pass", "name": "Иван Иванов", "groups": ["llm-engineers"]},
        "petrov": {"password": "pass", "name": "Пётр Петров", "groups": ["llm-users"]},
        "norole": {"password": "pass", "name": "Без Роли", "groups": ["unknown-group"]},
    }

    def authenticate(self, username: str, password: str) -> LdapIdentity:
        user = self.users.get(username.lower())
        if user is None or user["password"] != password:
            raise InvalidCredentials("bad credentials")
        return LdapIdentity(
            username=username.lower(),
            dn=f"CN={username},OU=Users,DC=corp,DC=local",
            display_name=user["name"],
            email=f"{username}@corp.local",
            groups=list(user["groups"]),
        )


class FakeLiteLLM:
    def __init__(self) -> None:
        self.keys: dict[str, dict] = {}
        self.deleted: list[str] = []
        self._counter = 0

    async def generate_key(self, payload: dict) -> dict:
        self._counter += 1
        token = "hash" + secrets.token_hex(8)
        key = "sk-" + secrets.token_urlsafe(24)
        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        self.keys[token] = {
            "token": token,
            "user_id": payload["user_id"],
            "key_alias": payload["key_alias"],
            "models": payload["models"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires": expires.isoformat(),
            "spend": 0.0,
            "blocked": False,
        }
        return {"key": key, "token": token, "expires": expires.isoformat()}

    async def list_keys(self, user_id: str, *, limit: int = 100) -> list[dict]:
        return [dict(item) for item in self.keys.values() if item["user_id"] == user_id][:limit]

    async def delete_key(self, litellm_token: str) -> None:
        self.deleted.append(litellm_token)
        self.keys.pop(litellm_token, None)

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture()
def env():
    settings = make_settings()
    fake_litellm = FakeLiteLLM()
    application = create_app(
        settings,
        ldap=FakeLdap(),
        litellm=fake_litellm,
        catalog=ModelCatalog.load(settings.catalog_path),
        policies=PolicySet.load(settings.policy_path),
        templates=build_templates("UTC"),
    )
    with TestClient(application) as client:
        yield client, fake_litellm


def login(client: TestClient, username: str = "ivanov", password: str = "pass"):
    return client.post(
        "/login",
        data={"username": username, "password": password, "next": "/"},
        follow_redirects=False,
    )


def csrf(client: TestClient) -> str:
    match = CSRF_RE.search(client.get("/").text)
    assert match, "CSRF-токен не найден на странице"
    return match.group(1)


def test_anonymous_is_redirected_to_login(env) -> None:
    client, _ = env
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_login_success_sets_cookie(env) -> None:
    client, _ = env
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "llmgate_session" in response.cookies
    page = client.get("/")
    assert page.status_code == 200
    assert "Ключ доступа" in page.text
    assert "Разработчик" in page.text


def test_login_wrong_password(env) -> None:
    client, _ = env
    response = login(client, password="wrong")
    assert response.status_code == 401
    assert "Неверный логин или пароль" in response.text
    assert "llmgate_session" not in response.cookies


def test_login_without_policy_is_forbidden(env) -> None:
    client, _ = env
    response = login(client, username="norole")
    assert response.status_code == 403
    assert "нет доступа" in response.text


def test_login_is_throttled(env) -> None:
    client, _ = env
    for _ in range(3):
        assert login(client, password="wrong").status_code == 401
    blocked = login(client, password="wrong")
    assert blocked.status_code == 429
    assert "Слишком много" in blocked.text


def test_tampered_cookie_is_rejected(env) -> None:
    client, _ = env
    login(client)
    client.cookies.set("llmgate_session", "definitely-not-a-valid-token")
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303


def test_create_key_returns_secret_once(env) -> None:
    client, litellm = env
    login(client)
    response = client.post("/keys", data={"_csrf": csrf(client), "alias": "jupyter"})
    assert response.status_code == 201
    assert "sk-" in response.text
    assert "Скопируйте ключ сейчас" in response.text
    assert len(litellm.keys) == 1
    stored = next(iter(litellm.keys.values()))
    assert stored["key_alias"] == "jupyter"
    assert stored["models"] == ["qwen3-32b"]

    page = client.get("/")
    assert "jupyter" in page.text
    # значение ключа не показывается повторно
    assert "Скопируйте ключ сейчас" not in page.text


def test_key_limit_is_enforced(env) -> None:
    client, _ = env
    login(client, username="petrov")  # политика viewer: max_active_keys = 1
    first = client.post("/keys", data={"_csrf": csrf(client)})
    assert first.status_code == 201
    second = client.post("/keys", data={"_csrf": csrf(client)})
    assert second.status_code == 409
    assert "лимит" in second.text


def test_csrf_is_required(env) -> None:
    client, _ = env
    login(client)
    response = client.post("/keys", data={"alias": "no-token"})
    assert response.status_code == 403
    assert "защитный токен" in response.text


def test_revoke_key(env) -> None:
    client, litellm = env
    login(client)
    client.post("/keys", data={"_csrf": csrf(client), "alias": "to-revoke"})
    token = next(iter(litellm.keys))

    response = client.post(f"/keys/{token}/revoke", data={"_csrf": csrf(client)}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/?msg=revoked"
    assert token in litellm.deleted
    assert token not in litellm.keys
    page = client.get("/?msg=revoked").text
    assert "Ключ отозван." in page
    assert "to-revoke" not in page


def test_cannot_revoke_foreign_key(env) -> None:
    client, litellm = env
    login(client)
    client.post("/keys", data={"_csrf": csrf(client)})
    foreign_token = next(iter(litellm.keys))

    client.cookies.clear()
    login(client, username="petrov")
    response = client.post(f"/keys/{foreign_token}/revoke", data={"_csrf": csrf(client)})
    assert response.status_code == 404
    assert foreign_token in litellm.keys


def test_logout_clears_session(env) -> None:
    client, _ = env
    login(client)
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_security_headers(env) -> None:
    client, _ = env
    response = client.get("/login")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_health_endpoints(env) -> None:
    client, _ = env
    assert client.get("/healthz").json()["status"] == "ok"
    ready = client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["ready"] is True
