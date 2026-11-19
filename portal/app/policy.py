"""Политики доступа: какие LDAP-группы что получают.

Роль — это блок в config/access.yaml. Правки версионируются в git и
применяются перезапуском портала (docker compose restart portal).
Отдельной админки ролей нет специально: она была бы вторым источником
истины рядом с LiteLLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
DURATIONS = {"1d", "7d", "30d"}


class PolicyError(Exception):
    pass


def normalize_group(value: str) -> str:
    return value.strip().lower()


def group_candidates(value: str) -> set[str]:
    """Из DN вида CN=LLM-Users,OU=Groups,... достаём и полный DN, и CN.

    Так политика срабатывает независимо от того, что вернул LDAP:
    атрибут memberOf (полный DN) или отдельный поиск по группам (CN).
    """
    normalized = normalize_group(value)
    candidates = {normalized}
    first_rdn = normalized.split(",", 1)[0]
    if "=" in first_rdn:
        cn = first_rdn.split("=", 1)[1].strip()
        if cn:
            candidates.add(cn)
    return candidates


@dataclass(frozen=True)
class Policy:
    id: str
    name: str
    ldap_groups: tuple[str, ...]
    models: tuple[str, ...]
    key_ttl_hours: int
    max_active_keys: int
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    max_parallel_requests: int | None = None
    budget_usd: float | None = None
    budget_duration: str = "30d"

    @property
    def allows_all_models(self) -> bool:
        return "*" in self.models


class PolicySet:
    def __init__(self, policies: list[Policy], overrides: dict[str, str], default_ttl: int) -> None:
        self._policies = policies
        self._by_id = {p.id: p for p in policies}
        self._overrides = {k.lower(): v for k, v in overrides.items()}
        self.default_ttl = default_ttl

    @classmethod
    def load(cls, path: Path) -> "PolicySet":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except FileNotFoundError as exc:
            raise PolicyError(f"файл политик не найден: {path}") from exc
        except yaml.YAMLError as exc:
            raise PolicyError(f"{path}: некорректный YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise PolicyError(f"{path}: ожидался YAML-объект")

        defaults = raw.get("defaults") or {}
        default_ttl = int(defaults.get("key_ttl_hours", 24))
        items = raw.get("policies") or []
        if not isinstance(items, list) or not items:
            raise PolicyError(f"{path}: секция policies пуста")

        policies: list[Policy] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise PolicyError(f"{path}: policies[{index}] должен быть объектом")
            policy_id = str(item.get("id", "")).strip()
            if not ID_RE.match(policy_id):
                raise PolicyError(f"{path}: id {policy_id!r} должен соответствовать {ID_RE.pattern}")
            if policy_id in seen:
                raise PolicyError(f"{path}: дубликат id {policy_id!r}")
            seen.add(policy_id)

            groups = tuple(str(g).strip() for g in (item.get("ldap_groups") or []) if str(g).strip())
            if not groups:
                raise PolicyError(f"{path}: политика {policy_id!r} без ldap_groups")

            models = tuple(str(m).strip() for m in (item.get("models") or []) if str(m).strip())
            if not models:
                raise PolicyError(f"{path}: политика {policy_id!r} без models")

            ttl = int(item.get("key_ttl_hours", default_ttl))
            if not 1 <= ttl <= 720:
                raise PolicyError(f"{path}: {policy_id!r}: key_ttl_hours должен быть 1..720")

            budget = item.get("budget_usd")
            duration = str(item.get("budget_duration") or "30d")
            if duration not in DURATIONS:
                raise PolicyError(f"{path}: {policy_id!r}: budget_duration из {sorted(DURATIONS)}")

            policies.append(
                Policy(
                    id=policy_id,
                    name=str(item.get("name") or policy_id),
                    ldap_groups=groups,
                    models=models,
                    key_ttl_hours=ttl,
                    max_active_keys=max(1, int(item.get("max_active_keys", 2))),
                    rpm_limit=_opt_int(item.get("rpm_limit")),
                    tpm_limit=_opt_int(item.get("tpm_limit")),
                    max_parallel_requests=_opt_int(item.get("max_parallel_requests")),
                    budget_usd=None if budget is None else float(budget),
                    budget_duration=duration,
                )
            )

        overrides = {str(k): str(v) for k, v in (raw.get("overrides") or {}).items()}
        unknown = [v for v in overrides.values() if v not in seen]
        if unknown:
            raise PolicyError(f"{path}: overrides ссылаются на неизвестные роли: {sorted(set(unknown))}")

        return cls(policies, overrides, default_ttl)

    def all(self) -> list[Policy]:
        return list(self._policies)

    def get(self, policy_id: str) -> Policy | None:
        return self._by_id.get(policy_id)

    def resolve(self, username: str, groups: list[str] | tuple[str, ...]) -> Policy | None:
        override = self._overrides.get(username.lower())
        if override:
            return self._by_id.get(override)
        user_groups: set[str] = set()
        for group in groups:
            if group:
                user_groups |= group_candidates(group)
        for policy in self._policies:
            allowed: set[str] = set()
            for group in policy.ldap_groups:
                allowed |= group_candidates(group)
            if "*" in allowed or allowed & user_groups:
                return policy
        return None

    def resolve_models(self, policy: Policy, catalog) -> list[str]:  # noqa: ANN001
        """Имена моделей (served_name), доступные роли и включённые на стенде."""
        return catalog.served_names(list(policy.models))


def _opt_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    number = int(value)
    return number if number > 0 else None


def build_key_payload(
    *,
    username: str,
    groups: list[str] | tuple[str, ...],
    policy: Policy,
    served_models: list[str],
    key_alias: str,
) -> dict:
    if not served_models:
        raise ValueError("роль не даёт доступа ни к одной включённой модели")

    payload: dict = {
        "duration": f"{policy.key_ttl_hours}h",
        "models": list(served_models),
        "key_alias": key_alias[:120],
        "user_id": username,
        "metadata": {
            "portal_user": username,
            "portal_policy": policy.id,
            "ldap_groups": list(groups)[:50],
            "issued_by": "llmgate-portal",
        },
    }
    if policy.rpm_limit:
        payload["rpm_limit"] = policy.rpm_limit
    if policy.tpm_limit:
        payload["tpm_limit"] = policy.tpm_limit
    if policy.max_parallel_requests:
        payload["max_parallel_requests"] = policy.max_parallel_requests
    if policy.budget_usd is not None:
        payload["max_budget"] = float(policy.budget_usd)
        payload["budget_duration"] = policy.budget_duration
    return payload
