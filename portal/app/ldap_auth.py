"""Аутентификация сотрудников в LDAP / Active Directory.

Схема:
  1. Подключаемся сервисной учётной записью (LDAP_BIND_DN).
  2. Ищем пользователя по фильтру (LDAP_USER_FILTER, {username} экранируется).
  3. Пробуем bind от имени найденного DN с введённым паролем — это и есть проверка.
  4. Собираем группы: из атрибута memberOf или поиском по группам.

Пароль сотрудника нигде не сохраняется и не логируется.
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass, field

from ldap3 import AUTO_BIND_NO_TLS, AUTO_BIND_TLS_BEFORE_BIND, NONE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPBindError, LDAPException, LDAPSocketOpenError
from ldap3.utils.conv import escape_filter_chars

from .settings import LdapSettings

log = logging.getLogger(__name__)


class LdapError(Exception):
    """Ошибка, которую можно показать пользователю."""


class InvalidCredentials(LdapError):
    pass


class LdapUnavailable(LdapError):
    pass


@dataclass
class LdapIdentity:
    username: str
    dn: str
    display_name: str = ""
    email: str = ""
    groups: list[str] = field(default_factory=list)


def normalize_group(value: str) -> str:
    """Приводим DN/CN к нижнему регистру, чтобы сравнение было устойчивым."""
    return value.strip().lower()


def cn_of(dn: str) -> str:
    first = dn.split(",", 1)[0]
    if "=" in first:
        return first.split("=", 1)[1].strip()
    return first.strip()


class LdapAuthenticator:
    def __init__(self, settings: LdapSettings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ API

    def authenticate(self, username: str, password: str) -> LdapIdentity:
        username = (username or "").strip()
        password = password or ""
        if not username or len(username) > 128:
            raise InvalidCredentials("Неверный логин или пароль")
        if not password or len(password) > 1024:
            raise InvalidCredentials("Неверный логин или пароль")

        entry = self._find_user(username)
        if entry is None:
            raise InvalidCredentials("Неверный логин или пароль")

        user_dn, attributes = entry
        self._verify_password(user_dn, password)

        groups = self._collect_groups(user_dn, attributes, username)
        display_name = self._first(attributes, self.settings.display_name_attr) or cn_of(user_dn)
        email = self._first(attributes, self.settings.email_attr) or ""

        return LdapIdentity(
            username=username.lower(),
            dn=user_dn,
            display_name=str(display_name),
            email=str(email),
            groups=sorted(groups),
        )

    # ------------------------------------------------------------ внутреннее

    def _server(self) -> Server:
        validate = ssl.CERT_REQUIRED if self.settings.tls_verify else ssl.CERT_NONE
        tls = Tls(validate=validate)
        return Server(
            self.settings.uri,
            get_info=NONE,
            tls=tls,
            connect_timeout=self.settings.timeout,
        )

    def _connect(self, user: str, password: str) -> Connection:
        auto_bind = AUTO_BIND_TLS_BEFORE_BIND if self.settings.start_tls else AUTO_BIND_NO_TLS
        try:
            return Connection(
                self._server(),
                user=user,
                password=password,
                auto_bind=auto_bind,
                raise_exceptions=True,
                read_only=True,
                receive_timeout=self.settings.timeout,
            )
        except LDAPSocketOpenError as exc:
            log.warning("LDAP недоступен (%s): %s", self.settings.uri, exc)
            raise LdapUnavailable("Сервер LDAP недоступен. Обратитесь к администратору.") from exc
        except LDAPBindError as exc:
            raise InvalidCredentials("Неверный логин или пароль") from exc
        except LDAPException as exc:
            log.warning("Ошибка подключения к LDAP: %s", exc)
            raise LdapUnavailable("Ошибка подключения к LDAP.") from exc

    def _find_user(self, username: str) -> tuple[str, dict[str, list[str]]] | None:
        conn = self._connect(self.settings.bind_dn, self.settings.bind_password)
        try:
            query = self.settings.user_filter.replace("{username}", escape_filter_chars(username))
            attributes = [
                self.settings.display_name_attr,
                self.settings.email_attr,
                self.settings.group_attr,
            ]
            ok = conn.search(
                search_base=self.settings.user_base,
                search_filter=query,
                search_scope=SUBTREE,
                attributes=[a for a in dict.fromkeys(attributes) if a],
                size_limit=3,
                time_limit=self.settings.timeout,
            )
            if not ok or not conn.entries:
                return None
            if len(conn.entries) > 1:
                log.warning("LDAP: по фильтру найдено несколько пользователей для %r", username)
                return None
            entry = conn.entries[0]
            values: dict[str, list[str]] = {}
            for name in attributes:
                if not name:
                    continue
                try:
                    attribute = entry[name]
                except (KeyError, LDAPException):
                    continue
                if attribute is None:
                    continue
                values[name] = [self._decode(v) for v in attribute.values]
            return entry.entry_dn, values
        except LDAPException as exc:
            log.warning("LDAP: ошибка поиска пользователя: %s", exc)
            raise LdapUnavailable("Ошибка поиска в LDAP.") from exc
        finally:
            conn.unbind()

    def _verify_password(self, user_dn: str, password: str) -> None:
        try:
            conn = self._connect(user_dn, password)
        except InvalidCredentials:
            raise
        except LdapUnavailable:
            raise
        else:
            conn.unbind()

    def _collect_groups(
        self, user_dn: str, attributes: dict[str, list[str]], username: str
    ) -> set[str]:
        groups: set[str] = set()
        if self.settings.group_attr:
            for value in attributes.get(self.settings.group_attr, []):
                if value:
                    groups.add(normalize_group(value))
                    groups.add(normalize_group(cn_of(value)))

        if self.settings.group_base and self.settings.group_filter:
            direct = self._search_groups(
                self.settings.group_filter.replace("{dn}", escape_filter_chars(user_dn)).replace(
                    "{username}", escape_filter_chars(username)
                )
            )
            groups.update(direct)

            if self.settings.nested_groups:
                chain_filter = (
                    "(member:1.2.840.113556.1.4.1941:=" + escape_filter_chars(user_dn) + ")"
                )
                groups.update(self._search_groups(chain_filter))

        return {g for g in groups if g}

    def _search_groups(self, query: str) -> set[str]:
        try:
            conn = self._connect(self.settings.bind_dn, self.settings.bind_password)
        except LdapError:
            return set()
        found: set[str] = set()
        try:
            ok = conn.search(
                search_base=self.settings.group_base,
                search_filter=query,
                search_scope=SUBTREE,
                attributes=["cn", "distinguishedName"],
                size_limit=200,
                time_limit=self.settings.timeout,
            )
            if not ok:
                return found
            for entry in conn.entries:
                found.add(normalize_group(entry.entry_dn))
                try:
                    cn = entry["cn"].value
                except (KeyError, LDAPException):
                    cn = None
                if cn:
                    found.add(normalize_group(str(cn)))
        except LDAPException as exc:
            log.warning("LDAP: ошибка поиска групп: %s", exc)
        finally:
            conn.unbind()
        return found

    @staticmethod
    def _first(attributes: dict[str, list[str]], name: str) -> str | None:
        values = attributes.get(name) or []
        return values[0] if values else None

    @staticmethod
    def _decode(value: object) -> str:
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                return value.decode("latin-1", errors="replace")
        return str(value)
