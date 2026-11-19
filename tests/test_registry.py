"""Тесты реестра моделей и генератора конфигурации."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

import llmgate

ROOT = Path(__file__).resolve().parents[1]


def test_repository_registry_is_valid() -> None:
    models = llmgate.load_registry()
    errors, _warnings = llmgate.validate(models)
    assert errors == []
    names = {m.name for m in models}
    assert {"qwen3-32b", "qwen3-8b", "bge-m3", "llm2-qwen3-32b"} <= names


def test_generated_files_are_up_to_date() -> None:
    rendered = llmgate._render_all()
    for path, content in rendered.items():
        assert path.read_text(encoding="utf-8") == content, f"устарел {path}"


def test_compose_contains_service_and_gpu_reservation() -> None:
    models = llmgate.load_registry()
    settings = llmgate.load_registry_settings()
    compose = yaml.safe_load(llmgate.render_compose(models, settings["defaults"]))
    service = compose["services"]["sglang-qwen3-32b"]
    assert service["image"].startswith("lmsysorg/sglang:")
    assert "--tp" in service["command"]
    assert service["command"][service["command"].index("--tp") + 1] == "2"
    assert service["deploy"]["resources"]["reservations"]["devices"][0]["device_ids"] == ["0", "1"]


def test_disabled_models_are_not_in_compose() -> None:
    models = llmgate.load_registry()
    settings = llmgate.load_registry_settings()
    compose = yaml.safe_load(llmgate.render_compose(models, settings["defaults"]))
    assert "sglang-qwen3-8b" not in compose["services"]


def test_litellm_has_alias_and_env_key() -> None:
    models = llmgate.load_registry()
    settings = llmgate.load_registry_settings()
    doc = yaml.safe_load(llmgate.render_litellm(models, settings["router"], {}))
    names = [entry["model_name"] for entry in doc["model_list"]]
    assert "qwen3-32b" in names and "qwen3" in names
    entry = next(e for e in doc["model_list"] if e["model_name"] == "qwen3-32b")
    assert entry["litellm_params"]["api_key"] == "os.environ/SGLANG_API_KEY"
    assert entry["litellm_params"]["api_base"].endswith(":30000/v1")


def test_fallbacks_render_only_enabled_targets() -> None:
    models = llmgate.load_registry()
    settings = llmgate.load_registry_settings()
    doc_off = yaml.safe_load(llmgate.render_litellm(models, settings["router"], {}))
    assert "fallbacks" not in doc_off["router_settings"]

    for model in models:
        if model.name == "qwen3-8b":
            model.enabled = True
    doc_on = yaml.safe_load(llmgate.render_litellm(models, settings["router"], {}))
    assert {"qwen3-32b": ["qwen3-8b"]} in doc_on["router_settings"]["fallbacks"]


def test_cycle_is_reported() -> None:
    a = llmgate.Model(name="a", kind="SGLangModel", title="a", fallbacks=["b"])
    b = llmgate.Model(name="b", kind="SGLangModel", title="b", fallbacks=["a"])
    errors, _ = llmgate.validate([a, b])
    assert any("цикл" in e for e in errors)


def test_duplicate_names_are_reported() -> None:
    a = llmgate.Model(name="dup", kind="SGLangModel", title="a")
    b = llmgate.Model(name="dup", kind="SGLangModel", title="b")
    errors, _ = llmgate.validate([a, b])
    assert any("дубликат" in e for e in errors)


def test_unknown_fallback_is_reported() -> None:
    a = llmgate.Model(name="a", kind="SGLangModel", title="a", fallbacks=["ghost"])
    errors, _ = llmgate.validate([a])
    assert any("не найдена" in e for e in errors)


def test_catalog_json_is_valid() -> None:
    payload = json.loads((ROOT / "config" / "models.generated.json").read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    names = {m["name"] for m in payload["models"]}
    assert "qwen3-32b" in names
