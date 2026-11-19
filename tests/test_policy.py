"""Тесты политик доступа и сборки payload для LiteLLM."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.policy import PolicyError, PolicySet, build_key_payload, normalize_group

ROOT = Path(__file__).resolve().parents[1]
ACCESS = ROOT / "config" / "access.yaml"


@pytest.fixture()
def policies() -> PolicySet:
    return PolicySet.load(ACCESS)


def test_resolve_by_group(policies: PolicySet) -> None:
    policy = policies.resolve("ivanov", ["llm-engineers"])
    assert policy is not None and policy.id == "engineer"


def test_resolve_is_case_insensitive(policies: PolicySet) -> None:
    assert policies.resolve("ivanov", ["CN=LLM-ADMINS,OU=Groups"]).id == "admin"


def test_unknown_groups_deny(policies: PolicySet) -> None:
    assert policies.resolve("ivanov", ["some-other-group"]) is None


def test_wildcard_group_matches_everyone() -> None:
    path = ROOT / "tests" / "_tmp_access.yaml"
    path.write_text(
        "policies:\n  - id: all\n    ldap_groups: ['*']\n    models: ['*']\n",
        encoding="utf-8",
    )
    try:
        loaded = PolicySet.load(path)
        assert loaded.resolve("anyone", []).id == "all"
    finally:
        path.unlink(missing_ok=True)


def test_override_wins_over_groups() -> None:
    path = ROOT / "tests" / "_tmp_override.yaml"
    path.write_text(
        "policies:\n"
        "  - id: viewer\n    ldap_groups: ['llm-users']\n    models: ['m']\n"
        "  - id: admin\n    ldap_groups: ['llm-admins']\n    models: ['*']\n"
        "overrides:\n  ivanov: admin\n",
        encoding="utf-8",
    )
    try:
        loaded = PolicySet.load(path)
        assert loaded.resolve("Ivanov", ["llm-users"]).id == "admin"
    finally:
        path.unlink(missing_ok=True)


def test_invalid_policies_raise() -> None:
    path = ROOT / "tests" / "_tmp_bad.yaml"
    path.write_text("policies:\n  - id: Bad_ID\n    ldap_groups: ['x']\n    models: ['m']\n", encoding="utf-8")
    try:
        with pytest.raises(PolicyError):
            PolicySet.load(path)
    finally:
        path.unlink(missing_ok=True)


def test_key_payload_contains_limits(policies: PolicySet) -> None:
    policy = policies.get("engineer")
    payload = build_key_payload(
        username="ivanov",
        groups=["llm-engineers"],
        policy=policy,
        served_models=["qwen3-32b", "bge-m3"],
        key_alias="jupyter",
    )
    assert payload["duration"] == "24h"
    assert payload["models"] == ["qwen3-32b", "bge-m3"]
    assert payload["user_id"] == "ivanov"
    assert payload["rpm_limit"] == 120
    assert payload["metadata"]["portal_policy"] == "engineer"


def test_key_payload_requires_models(policies: PolicySet) -> None:
    with pytest.raises(ValueError):
        build_key_payload(
            username="ivanov",
            groups=[],
            policy=policies.get("viewer"),
            served_models=[],
            key_alias="x",
        )


def test_normalize_group() -> None:
    assert normalize_group(" CN=LLM-Users,OU=Groups ") == "cn=llm-users,ou=groups"
