"""Тесты реестра, резервных каналов и генератора.

По умолчанию реестр пуст — проверяем это, а рабочие сценарии гоняем
на примерах из examples/models, которые в деплой не попадают.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import llmgate

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "models"


def example_models() -> list[llmgate.Model]:
    return llmgate.load_registry(EXAMPLES)


def enabled_examples() -> list[llmgate.Model]:
    models = example_models()
    for model in models:
        model.enabled = True
    return models


def test_registry_is_empty_by_default() -> None:
    assert llmgate.load_registry() == []


def test_render_fails_with_clear_error_on_empty_registry() -> None:
    with pytest.raises(llmgate.RegistryError):
        llmgate._render_all()


def test_examples_are_valid() -> None:
    errors, _warnings = llmgate.validate(example_models(), llmgate.load_router_config())
    assert errors == []
    assert {"qwen3-32b", "qwen3-8b", "bge-m3", "llm2-qwen3-32b"} <= {m.name for m in example_models()}


def test_no_fallbacks_inside_model_files() -> None:
    """Резервы живут только в config/router.yaml — один источник правды."""
    for folder in (ROOT / "models", EXAMPLES):
        for path in sorted(folder.glob("*/model.yaml")):
            spec = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("spec") or {}
            assert "fallbacks" not in spec, f"{path}: резервы должны быть в router.yaml"


def test_compose_from_examples() -> None:
    compose = yaml.safe_load(
        llmgate.render_compose(example_models(), llmgate.load_registry_settings())
    )
    service = compose["services"]["sglang-qwen3-32b"]
    assert service["image"].startswith("lmsysorg/sglang:")
    assert service["command"][service["command"].index("--tp") + 1] == "2"
    assert service["deploy"]["resources"]["reservations"]["devices"][0]["device_ids"] == ["0", "1"]
    assert "sglang-qwen3-8b" not in compose["services"]


def test_litellm_from_examples() -> None:
    doc = yaml.safe_load(llmgate.render_litellm(example_models(), llmgate.load_router_config(), {}))
    names = [entry["model_name"] for entry in doc["model_list"]]
    assert "qwen3-32b" in names and "qwen3" in names
    entry = next(e for e in doc["model_list"] if e["model_name"] == "qwen3-32b")
    assert entry["litellm_params"]["api_key"] == "os.environ/SGLANG_API_KEY"
    assert entry["litellm_params"]["api_base"].endswith(":30000/v1")


def test_router_settings_are_copied_from_native_file() -> None:
    doc = yaml.safe_load(
        llmgate.render_litellm(example_models(), llmgate.load_router_config(), {})
    )
    router = doc["router_settings"]
    assert router["num_retries"] == 2
    assert router["timeout"] == 600
    assert router["routing_strategy"] == "simple-shuffle"


def test_fallbacks_render_in_litellm_format() -> None:
    router = llmgate.load_router_config() | {"fallbacks": [{"qwen3-32b": ["qwen3-8b"]}]}
    doc = yaml.safe_load(llmgate.render_litellm(enabled_examples(), router, {}))
    assert {"qwen3-32b": ["qwen3-8b"]} in doc["router_settings"]["fallbacks"]


def test_fallback_accepts_registry_name_and_renders_api_name() -> None:
    models = example_models()
    for model in models:
        if model.kind == "RemoteModel":
            model.enabled = True
    router = llmgate.load_router_config() | {"fallbacks": [{"qwen3-32b": ["llm2-qwen3-32b"]}]}
    doc = yaml.safe_load(llmgate.render_litellm(models, router, {}))
    assert doc["router_settings"]["fallbacks"] == [{"qwen3-32b": ["qwen3-32b-remote"]}]


def test_disabled_fallback_is_skipped_with_warning(capsys: pytest.CaptureFixture[str]) -> None:
    router = llmgate.load_router_config() | {"fallbacks": [{"qwen3-32b": ["qwen3-8b"]}]}
    doc = yaml.safe_load(llmgate.render_litellm(example_models(), router, {}))
    assert "fallbacks" not in doc["router_settings"]
    assert "выключен и пропущен" in capsys.readouterr().err


def test_cycle_is_reported() -> None:
    a = llmgate.Model(name="a", kind="SGLangModel", title="a")
    b = llmgate.Model(name="b", kind="SGLangModel", title="b")
    errors, _ = llmgate.validate([a, b], {"fallbacks": [{"a": ["b"]}, {"b": ["a"]}]})
    assert any("цикл" in error for error in errors)


def test_unknown_fallback_is_reported() -> None:
    a = llmgate.Model(name="a", kind="SGLangModel", title="a")
    errors, _ = llmgate.validate([a], {"fallbacks": [{"a": ["ghost"]}]})
    assert any("не найден" in error for error in errors)


def test_duplicate_names_are_reported() -> None:
    a = llmgate.Model(name="dup", kind="SGLangModel", title="a")
    b = llmgate.Model(name="dup", kind="SGLangModel", title="b")
    errors, _ = llmgate.validate([a, b])
    assert any("дубликат" in error for error in errors)


def test_minimal_model_file_works() -> None:
    """Минимальная модель — четыре строки: всё остальное имеет значения по умолчанию."""
    target = ROOT / "tests" / "minimal" / "model.yaml"
    target.parent.mkdir(exist_ok=True)
    target.write_text(
        "kind: SGLangModel\nspec:\n  sglang:\n    hf_repo: org/model\n", encoding="utf-8"
    )
    try:
        model = llmgate.load_model_file(target)
        assert model.name == "minimal"
        assert model.served_name == "minimal"
        assert model.enabled is True
        assert model.context_length == 32768
        args = llmgate._sglang_args(model)
        assert args[args.index("--tp") + 1] == "1"
    finally:
        target.unlink()
        target.parent.rmdir()


def test_catalog_from_examples() -> None:
    payload = json.loads(llmgate.render_catalog(example_models(), {}))
    assert payload["schema"] == 1
    names = {m["name"] for m in payload["models"]}
    assert {"qwen3-32b", "qwen3-8b", "bge-m3", "llm2-qwen3-32b"} == names
