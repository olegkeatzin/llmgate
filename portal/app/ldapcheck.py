"""Диагностика LDAP изнутри контейнера.

    docker compose exec -it portal python -m app.ldapcheck ivanov

Скрипт проверяет bind сервисной учёткой, поиск пользователя, bind
самим пользователем, список групп и то, какая роль ему достанется.
Пароль спрашивается интерактивно и нигде не сохраняется.
"""

from __future__ import annotations

import getpass
import sys

from .ldap_auth import InvalidCredentials, LdapAuthenticator, LdapError, LdapUnavailable
from .policy import PolicySet
from .settings import Settings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Использование: python -m app.ldapcheck <логин>", file=sys.stderr)
        return 2

    settings = Settings.from_env()
    username = argv[1]
    password = getpass.getpass("Пароль LDAP: ")
    auth = LdapAuthenticator(settings.ldap)

    try:
        identity = auth.authenticate(username, password)
    except InvalidCredentials:
        print("ОШИБКА: неверный логин или пароль")
        return 1
    except LdapUnavailable as exc:
        print(f"ОШИБКА: LDAP недоступен: {exc}")
        return 1
    except LdapError as exc:
        print(f"ОШИБКА LDAP: {exc}")
        return 1

    print(f"OK: {identity.username} — {identity.display_name}")
    print(f"DN: {identity.dn}")
    print("Группы:")
    if not identity.groups:
        print("  (пусто — проверьте LDAP_GROUP_ATTR / LDAP_GROUP_BASE)")
    for group in identity.groups:
        print("  - " + group)

    policies = PolicySet.load(settings.policy_path)
    policy = policies.resolve(identity.username, identity.groups)
    if policy is None:
        print("\nРОЛЬ НЕ НАЙДЕНА: пользователь войдёт, но ключ не получит.")
        print("Добавьте его группу в config/access.yaml.")
        return 3

    print(f"\nРоль: {policy.id} ({policy.name})")
    print(f"Модели: {', '.join(policy.models)}")
    print(
        f"Срок ключа: {policy.key_ttl_hours} ч, ключей одновременно: {policy.max_active_keys}, "
        f"rpm: {policy.rpm_limit or 'без лимита'}, tpm: {policy.tpm_limit or 'без лимита'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
