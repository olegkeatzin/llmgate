#!/usr/bin/env python3
"""LLMGate — реестр моделей и генератор конфигурации.

Модели описываются файлами models/<имя>/model.yaml. Команда render превращает
реестр в три артефакта:

  * docker-compose.models.yml      — контейнеры SGLang (по одному на модель);
  * config/litellm.generated.yaml  — model_list LiteLLM + резервные каналы;
  * config/models.generated.json   — каталог моделей для веб-портала.

Требуется только PyYAML:  python3 -m pip install pyyaml

Примеры:
    python3 tools/llmgate.py list
    python3 tools/llmgate.py validate
    python3 tools/llmgate.py render
    python3 tools/llmgate.py render --check
    python3 tools/llmgate.py add qwen2.5-72b --hf-repo Qwen/Qwen2.5-72B-Instruct --tp 2 --gpus 0,1
    python3 tools/llmgate.py doctor
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"
CONFIG_DIR = ROOT / "config"
COMPOSE_OUT = ROOT / "docker-compose.models.yml"
LITELLM_OUT = CONFIG_DIR / "litellm.generated.yaml"
CATALOG_OUT = CONFIG_DIR / "models.generated.json"
REGISTRY_SETTINGS = CONFIG_DIR / "llmgate.yaml"
LITELLM_SETTINGS = CONFIG_DIR / "litellm.settings.yaml"
ROUTER_SETTINGS = CONFIG_DIR / "router.yaml"

PROJECT_NAME = "llmgate"
SERVICE_PREFIX = "sglang-"
DEFAULT_CONTAINER_PORT = 30000
HF_CACHE_MOUNT = "/root/.cache/huggingface"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
VALID_CAPABILITIES = {"chat", "embedding", "vision", "rerank"}
VALID_KINDS = {"SGLangModel", "RemoteModel"}

GENERATED_HEADER = (
    "# ВНИМАНИЕ: файл сгенерирован tools/llmgate.py.\n"
    "# Не редактируйте вручную: измените models/<имя>/model.yaml\n"
    "# и выполните: make models\n"
)


class RegistryError(Exception):
    """Ошибка чтения или валидации реестра."""


# --------------------------------------------------------------------------- #
# Модель предметной области
# --------------------------------------------------------------------------- #


@dataclass
class Model:
    name: str
    kind: str
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    served_name: str = ""
    aliases: list[str] = field(default_factory=list)
    context_length: int = 32768
    max_output_tokens: int = 8192
    capabilities: list[str] = field(default_factory=lambda: ["chat"])
    pricing_input: float = 0.0
    pricing_output: float = 0.0
    sglang: dict[str, Any] = field(default_factory=dict)
    remote: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    @property
    def is_embedding(self) -> bool:
        return "embedding" in self.capabilities

    @property
    def service_name(self) -> str:
        return f"{SERVICE_PREFIX}{self.name}"

    def all_names(self) -> list[str]:
        names = [self.name, self.served_name, *self.aliases]
        return [n for n in dict.fromkeys(names) if n]


# --------------------------------------------------------------------------- #
# Чтение реестра
# --------------------------------------------------------------------------- #


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RegistryError(f"ожидалось целое число, получено {value!r}") from exc


def load_model_file(path: Path) -> Model:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RegistryError(f"{path}: некорректный YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise RegistryError(f"{path}: ожидался YAML-объект")

    kind = str(doc.get("kind", "")).strip()
    if kind not in VALID_KINDS:
        raise RegistryError(
            f"{path}: kind должен быть одним из {sorted(VALID_KINDS)}, получено {kind!r}"
        )

    meta = doc.get("metadata") or {}
    spec = doc.get("spec") or {}
    if not isinstance(meta, dict) or not isinstance(spec, dict):
        raise RegistryError(f"{path}: metadata и spec должны быть объектами")

    name = str(meta.get("name") or path.parent.name).strip()
    if not NAME_RE.match(name):
        raise RegistryError(
            f"{path}: имя {name!r} должно соответствовать {NAME_RE.pattern}"
        )

    pricing = spec.get("pricing") or {}
    model = Model(
        name=name,
        kind=kind,
        title=str(meta.get("title") or name),
        description=str(meta.get("description") or "").strip(),
        tags=[str(t) for t in (meta.get("tags") or [])],
        enabled=_as_bool(spec.get("enabled"), True),
        served_name=str(spec.get("served_name") or name).strip(),
        aliases=[str(a).strip() for a in (spec.get("aliases") or []) if str(a).strip()],
        context_length=_as_int(spec.get("context_length"), 32768),
        max_output_tokens=_as_int(spec.get("max_output_tokens"), 8192),
        capabilities=[str(c) for c in (spec.get("capabilities") or ["chat"])],
        pricing_input=float(pricing.get("input_per_1m_usd") or 0.0),
        pricing_output=float(pricing.get("output_per_1m_usd") or 0.0),
        sglang=dict(spec.get("sglang") or {}) if kind == "SGLangModel" else {},
        remote=dict(spec.get("remote") or {}) if kind == "RemoteModel" else {},
        source=str(path.relative_to(ROOT)),
    )

    if kind == "SGLangModel":
        s = model.sglang
        if not s.get("hf_repo") and not s.get("model_path"):
            raise RegistryError(f"{path}: укажите spec.sglang.hf_repo или spec.sglang.model_path")
        if s.get("local_dir") and not s.get("model_path"):
            raise RegistryError(f"{path}: local_dir требует spec.sglang.model_path (путь внутри контейнера)")
        if _as_int(s.get("tensor_parallel"), 1) < 1:
            raise RegistryError(f"{path}: tensor_parallel должен быть >= 1")
        gpus = str(s.get("gpus") or "").strip()
        if gpus:
            ids = [g.strip() for g in gpus.split(",") if g.strip()]
            tp = _as_int(s.get("tensor_parallel"), 1)
            if len(ids) < tp:
                raise RegistryError(
                    f"{path}: tensor_parallel={tp}, но перечислено GPU: {len(ids)} ({gpus})"
                )
            if not all(g.isdigit() for g in ids):
                raise RegistryError(f"{path}: gpus должен быть списком индексов, например \"0,1\"")
    else:
        r = model.remote
        if not r.get("base_url"):
            raise RegistryError(f"{path}: укажите spec.remote.base_url")
        if not r.get("upstream_model"):
            raise RegistryError(f"{path}: укажите spec.remote.upstream_model (например openai/qwen3-32b)")

    return model


def load_registry(models_dir: Path = MODELS_DIR) -> list[Model]:
    if not models_dir.is_dir():
        raise RegistryError(f"каталог моделей не найден: {models_dir}")
    models: list[Model] = []
    for path in sorted(models_dir.glob("*/model.yaml")):
        if path.parent.name.startswith("_"):
            continue
        models.append(load_model_file(path))
    return models


def load_registry_settings(path: Path = REGISTRY_SETTINGS) -> dict[str, Any]:
    defaults = {
        "image": "lmsysorg/sglang:v0.5.23-cu130",
        "hf_cache_dir": "./data/hf-cache",
    }
    if not path.exists():
        return defaults
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise RegistryError(f"{path}: ожидался YAML-объект")
    return {**defaults, **(doc.get("defaults") or {})}


ROUTER_DEFAULTS: dict[str, Any] = {
    "routing_strategy": "simple-shuffle",
    "num_retries": 2,
    "timeout": 600,
    "allowed_fails": 3,
    "cooldown_time": 45,
    "retry_after": 5,
    "fallbacks": [],
    "default_fallbacks": [],
}


def load_router_config(path: Path = ROUTER_SETTINGS) -> dict[str, Any]:
    """Читает config/router.yaml — это ровно блок router_settings LiteLLM."""
    if not path.exists():
        return dict(ROUTER_DEFAULTS)
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise RegistryError(f"{path}: ожидался YAML-объект")

    config = {**ROUTER_DEFAULTS, **doc}
    raw_fallbacks = config.get("fallbacks") or []
    if not isinstance(raw_fallbacks, list):
        raise RegistryError(f"{path}: fallbacks должен быть списком вида \"- model: [резерв]\"")
    for item in raw_fallbacks:
        if not isinstance(item, dict) or len(item) != 1:
            raise RegistryError(f"{path}: каждая запись fallbacks — объект с одной моделью")
        targets = next(iter(item.values()))
        if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
            raise RegistryError(f"{path}: резервы должны быть списком имён моделей")
    if not isinstance(config.get("default_fallbacks"), list):
        raise RegistryError(f"{path}: default_fallbacks должен быть списком")
    return config


def fallback_map(router: dict[str, Any]) -> dict[str, list[str]]:
    """{"модель": [резерв1, резерв2]} из нативного формата LiteLLM."""
    result: dict[str, list[str]] = {}
    for item in router.get("fallbacks") or []:
        for source, targets in item.items():
            result.setdefault(str(source), []).extend(str(t) for t in targets)
    return result


# --------------------------------------------------------------------------- #
# Валидация
# --------------------------------------------------------------------------- #


def validate(models: list[Model], router: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    router = router or {}

    by_name: dict[str, Model] = {}
    owner: dict[str, str] = {}

    for m in models:
        if m.name in by_name:
            errors.append(f"дубликат имени модели: {m.name} ({m.source})")
        by_name[m.name] = m

        for n in m.all_names():
            if n in owner and owner[n] != m.name:
                errors.append(
                    f"имя {n!r} используется и моделью {owner[n]!r}, и моделью {m.name!r}"
                )
            owner[n] = m.name

        if m.context_length < 512:
            errors.append(f"{m.name}: context_length слишком мал ({m.context_length})")
        if not m.is_embedding:
            if m.max_output_tokens <= 0:
                errors.append(f"{m.name}: max_output_tokens должен быть > 0")
            if m.max_output_tokens >= m.context_length:
                warnings.append(
                    f"{m.name}: max_output_tokens ({m.max_output_tokens}) >= context_length ({m.context_length})"
                )
        bad_caps = set(m.capabilities) - VALID_CAPABILITIES
        if bad_caps:
            errors.append(f"{m.name}: неизвестные capabilities {sorted(bad_caps)}")
        if m.is_embedding and m.capabilities != ["embedding"]:
            warnings.append(f"{m.name}: для embedding-модели обычно capabilities: [embedding]")
        if m.kind == "SGLangModel" and not m.enabled:
            warnings.append(f"{m.name}: выключена (enabled: false) и не попадёт в docker-compose")
        if m.pricing_input < 0 or m.pricing_output < 0:
            errors.append(f"{m.name}: цены не могут быть отрицательными")

    # Имя каталога, served_name и алиасы — всё допустимо писать в router.yaml.
    canonical = {n: m.served_name for m in models for n in m.all_names()}

    fallbacks = fallback_map(router)
    for source, targets in fallbacks.items():
        if source not in canonical:
            errors.append(
                f"router.yaml: {source!r} не найдена (известные имена: {', '.join(sorted(canonical)[:8])})"
            )
            continue
        for target in targets:
            if target not in canonical:
                errors.append(f"router.yaml: {source}: резерв {target!r} не найден в реестре")
                continue
            if canonical[target] == canonical[source]:
                errors.append(f"router.yaml: {source}: модель не может быть резервом сама для себя")
                continue
            if not by_name[owner[target]].enabled:
                warnings.append(
                    f"router.yaml: {source}: резерв {target!r} выключен (enabled: false) — "
                    f"будет исключён из LiteLLM"
                )

    for fb in router.get("default_fallbacks") or []:
        if fb not in canonical:
            errors.append(f"router.yaml: default_fallbacks содержит неизвестную модель {fb!r}")

    # Циклы в цепочках резерва
    graph = {
        canonical[source]: [canonical[t] for t in targets if t in canonical]
        for source, targets in fallbacks.items()
        if source in canonical
    }
    state: dict[str, int] = {}

    def visit(node: str, stack: list[str]) -> None:
        state[node] = 1
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                cycle = " → ".join([*stack, node, nxt])
                errors.append(f"цикл в резервных цепочках: {cycle}")
            elif state.get(nxt, 0) == 0:
                visit(nxt, [*stack, node])
        state[node] = 2

    for node in graph:
        if state.get(node, 0) == 0:
            visit(node, [])

    # Публикуемые порты не должны совпадать
    published: dict[int, str] = {}
    for m in models:
        port = _as_int(m.sglang.get("publish_port"), 0) if m.kind == "SGLangModel" else 0
        if port:
            if port in published:
                errors.append(f"порт {port} публикуют и {published[port]}, и {m.name}")
            published[port] = m.name

    if not any(m.enabled for m in models):
        errors.append("нет ни одной включённой модели (enabled: true)")

    return errors, warnings


# --------------------------------------------------------------------------- #
# Рендер docker-compose
# --------------------------------------------------------------------------- #


def _sglang_args(model: Model) -> list[str]:
    s = model.sglang
    model_path = str(s.get("model_path") or s.get("hf_repo"))
    port = _as_int(s.get("port"), DEFAULT_CONTAINER_PORT)
    tp = _as_int(s.get("tensor_parallel"), 1)
    mfs = s.get("mem_fraction_static", 0.85)

    args = [
        "python3",
        "-m",
        "sglang.launch_server",
        "--model-path",
        model_path,
        "--served-model-name",
        model.served_name,
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--tp",
        str(tp),
        "--mem-fraction-static",
        str(mfs),
        "--context-length",
        str(model.context_length),
    ]
    if s.get("dtype"):
        args += ["--dtype", str(s["dtype"])]
    if s.get("quantization"):
        args += ["--quantization", str(s["quantization"])]
    if s.get("revision") and s.get("hf_repo"):
        args += ["--revision", str(s["revision"])]
    if s.get("max_running_requests"):
        args += ["--max-running-requests", str(_as_int(s["max_running_requests"], 0))]
    if model.is_embedding:
        args.append("--is-embedding")
    if _as_bool(s.get("enable_metrics"), False):
        args.append("--enable-metrics")
    if _as_bool(s.get("trust_remote_code"), False):
        args.append("--trust-remote-code")
    for extra in s.get("extra_args") or []:
        args.append(str(extra))
    return args


def build_service(model: Model, defaults: dict[str, Any]) -> dict[str, Any]:
    s = model.sglang
    image = str(s.get("image") or defaults.get("image"))
    port = _as_int(s.get("port"), DEFAULT_CONTAINER_PORT)
    hf_cache = str(defaults.get("hf_cache_dir") or "./data/hf-cache")

    env: dict[str, str] = {
        "HF_HOME": HF_CACHE_MOUNT,
        "HF_HUB_ENABLE_HF_TRANSFER": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "NCCL_DEBUG": "WARN",
    }
    env.update({str(k): str(v) for k, v in (s.get("env") or {}).items()})

    volumes = [f"{hf_cache}:{HF_CACHE_MOUNT}"]
    if s.get("local_dir"):
        volumes.append(f"{s['local_dir']}:/models/{model.name}:ro")

    service: dict[str, Any] = {
        "image": image,
        "container_name": f"{PROJECT_NAME}-{model.service_name}",
        "restart": "unless-stopped",
        "ipc": "host",
        "shm_size": str(s.get("shm_size") or "32g"),
        "environment": env,
        "volumes": volumes,
        "command": _sglang_args(model),
        "healthcheck": {
            # python3 есть в образе SGLang всегда, curl — не факт.
            "test": [
                "CMD-SHELL",
                _healthcheck_cmd(port),
            ],
            "interval": "30s",
            "timeout": "10s",
            "retries": 3,
            "start_period": f"{_as_int(s.get('startup_timeout_s'), 1800)}s",
        },
        "deploy": {
            "resources": {
                "reservations": {
                    "devices": [_gpu_device(s)],
                }
            }
        },
    }

    publish_port = _as_int(s.get("publish_port"), 0)
    if publish_port:
        service["ports"] = [f"{publish_port}:{port}"]

    return service


def _healthcheck_cmd(port: int) -> str:
    script = (
        "import urllib.request,sys; "
        "sys.exit(0 if urllib.request.urlopen("
        f"'http://127.0.0.1:{port}/health', timeout=5).status == 200 else 1)"
    )
    return f'python3 -c "{script}"'


def _gpu_device(sglang: dict[str, Any]) -> dict[str, Any]:
    gpus = str(sglang.get("gpus") or "").strip()
    if gpus:
        ids = [g.strip() for g in gpus.split(",") if g.strip()]
        return {"driver": "nvidia", "device_ids": ids, "capabilities": ["gpu"]}
    return {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}


def render_compose(models: list[Model], defaults: dict[str, Any]) -> str:
    services: dict[str, Any] = {}
    for model in sorted(models, key=lambda m: m.name):
        if model.enabled and model.kind == "SGLangModel":
            services[model.service_name] = build_service(model, defaults)
    if not services:
        raise RegistryError("нет включённых SGLang-моделей — нечего запускать")
    doc = {"name": PROJECT_NAME, "services": services}
    return GENERATED_HEADER + yaml.safe_dump(
        doc, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000
    )


# --------------------------------------------------------------------------- #
# Рендер LiteLLM
# --------------------------------------------------------------------------- #


def _litellm_params(model: Model) -> dict[str, Any]:
    if model.kind == "SGLangModel":
        port = _as_int(model.sglang.get("port"), DEFAULT_CONTAINER_PORT)
        return {
            "model": f"openai/{model.served_name}",
            "api_base": f"http://{model.service_name}:{port}/v1",
            "api_key": "os.environ/SGLANG_API_KEY",
        }
    r = model.remote
    params: dict[str, Any] = {
        "model": str(r.get("upstream_model")),
        "api_base": str(r.get("base_url")),
        "api_key": f"os.environ/{r.get('api_key_env') or 'REMOTE_LLM_API_KEY'}",
    }
    if r.get("timeout_s"):
        params["timeout"] = _as_int(r["timeout_s"], 600)
    if r.get("organization"):
        params["organization"] = str(r["organization"])
    return params


def _model_info(model: Model) -> dict[str, Any]:
    info: dict[str, Any] = {
        "mode": "embedding" if model.is_embedding else "chat",
        "max_input_tokens": model.context_length,
        "input_cost_per_token": model.pricing_input / 1_000_000,
        "output_cost_per_token": model.pricing_output / 1_000_000,
    }
    if not model.is_embedding:
        info["max_output_tokens"] = model.max_output_tokens
        info["supports_function_calling"] = True
    return info


def render_litellm(
    models: list[Model],
    router_settings: dict[str, Any],
    static_settings: dict[str, Any],
) -> str:
    model_list: list[dict[str, Any]] = []
    for model in sorted(models, key=lambda m: m.name):
        if not model.enabled:
            continue
        entry = {
            "model_name": model.served_name,
            "litellm_params": _litellm_params(model),
            "model_info": _model_info(model),
        }
        model_list.append(entry)
        for alias in model.aliases:
            model_list.append(
                {
                    "model_name": alias,
                    "litellm_params": dict(entry["litellm_params"]),
                    "model_info": dict(entry["model_info"]),
                }
            )

    router: dict[str, Any] = dict(router_settings)
    router.pop("fallbacks", None)
    router.pop("default_fallbacks", None)

    enabled_names = {m.served_name for m in models if m.enabled}
    canonical = {n: m.served_name for m in models for n in m.all_names()}
    chains: list[dict[str, list[str]]] = []
    for source, targets in fallback_map(router_settings).items():
        api_source = canonical.get(source, source)
        if api_source not in enabled_names:
            continue
        normalized = [canonical.get(t, t) for t in targets]
        kept = [t for t in normalized if t in enabled_names and t != api_source]
        skipped = [t for t in normalized if t not in enabled_names]
        if skipped:
            print(
                f"  ! router.yaml: {source}: резерв {', '.join(skipped)} выключен и пропущен",
                file=sys.stderr,
            )
        if kept:
            chains.append({api_source: kept})
    if chains:
        router["fallbacks"] = chains

    default_fallbacks = [f for f in (router_settings.get("default_fallbacks") or []) if f in enabled_names]
    if default_fallbacks:
        router["default_fallbacks"] = default_fallbacks

    doc: dict[str, Any] = {"model_list": model_list, "router_settings": router}
    for key in ("general_settings", "litellm_settings"):
        if key in static_settings:
            doc[key] = static_settings[key]
    return GENERATED_HEADER + yaml.safe_dump(
        doc, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000
    )


# --------------------------------------------------------------------------- #
# Рендер каталога для портала
# --------------------------------------------------------------------------- #


def render_catalog(models: list[Model], fallbacks: dict[str, list[str]] | None = None) -> str:
    fallbacks = fallbacks or {}
    items = []
    for model in sorted(models, key=lambda m: m.name):
        items.append(
            {
                "name": model.name,
                "served_name": model.served_name,
                "aliases": model.aliases,
                "title": model.title,
                "description": model.description,
                "kind": model.kind,
                "enabled": model.enabled,
                "capabilities": model.capabilities,
                "context_length": model.context_length,
                "max_output_tokens": model.max_output_tokens,
                "tags": model.tags,
                "fallbacks": fallbacks.get(model.served_name, []),
                "pricing": {
                    "input_per_1m_usd": model.pricing_input,
                    "output_per_1m_usd": model.pricing_output,
                },
                "source": model.source,
            }
        )
    return json.dumps({"schema": 1, "models": items}, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #


def cmd_list(_: argparse.Namespace) -> int:
    models = load_registry()
    if not models:
        print("Реестр пуст: создайте models/<имя>/model.yaml")
        return 0
    chains = fallback_map(load_router_config())
    fmt = "{:<18} {:<16} {:<9} {:<6} {:<8} {:<24} {}"
    print(fmt.format("ИМЯ", "ТИП", "ВКЛ", "TP", "GPU", "СЕРВИС/URL", "РЕЗЕРВ"))
    print("-" * 108)
    for m in models:
        if m.kind == "SGLangModel":
            where = f"{m.service_name}:{_as_int(m.sglang.get('port'), DEFAULT_CONTAINER_PORT)}"
            tp = _as_int(m.sglang.get("tensor_parallel"), 1)
            gpus = m.sglang.get("gpus") or "all"
        else:
            where = str(m.remote.get("base_url"))
            tp, gpus = "-", "-"
        print(
            fmt.format(
                m.name,
                m.kind,
                "да" if m.enabled else "нет",
                tp,
                gpus,
                where[:24],
                ", ".join(chains.get(m.served_name) or chains.get(m.name) or []) or "—",
            )
        )
    return 0


def cmd_validate(_: argparse.Namespace) -> int:
    router = load_router_config()
    models = load_registry()
    errors, warnings = validate(models, router)
    for w in warnings:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {w}")
    for e in errors:
        print(f"ОШИБКА: {e}", file=sys.stderr)
    if errors:
        print(f"\nВалидация не пройдена: ошибок {len(errors)}, предупреждений {len(warnings)}", file=sys.stderr)
        return 1
    print(f"OK: моделей {len(models)}, включено {sum(1 for m in models if m.enabled)}, предупреждений {len(warnings)}")
    return 0


def _render_all() -> dict[Path, str]:
    settings = load_registry_settings()
    router = load_router_config()
    models = load_registry()
    errors, warnings = validate(models, router)
    for w in warnings:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {w}")
    if errors:
        raise RegistryError("; ".join(errors))
    static = {}
    if LITELLM_SETTINGS.exists():
        doc = yaml.safe_load(LITELLM_SETTINGS.read_text(encoding="utf-8")) or {}
        if not isinstance(doc, dict):
            raise RegistryError(f"{LITELLM_SETTINGS}: ожидался YAML-объект")
        static = doc
    return {
        COMPOSE_OUT: render_compose(models, settings),
        LITELLM_OUT: render_litellm(models, router, static),
        CATALOG_OUT: render_catalog(models, fallback_map(router)),
    }


def cmd_render(args: argparse.Namespace) -> int:
    try:
        rendered = _render_all()
    except RegistryError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1

    stale: list[Path] = []
    for path, content in rendered.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != content:
            stale.append(path)
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
                print(f"  записан {path.relative_to(ROOT)}")

    if args.check:
        if stale:
            print("Устаревшие артефакты: " + ", ".join(str(p.relative_to(ROOT)) for p in stale), file=sys.stderr)
            print("Выполните: make models", file=sys.stderr)
            return 1
        print("OK: сгенерированные файлы актуальны")
        return 0

    if not stale:
        print("OK: изменений нет")
    return 0


ADD_TEMPLATE = """apiVersion: llmgate/v1
kind: SGLangModel
metadata:
  name: {name}
  title: "{title}"
  description: "Опишите назначение модели и кому она нужна."
  tags: [chat]
spec:
  enabled: true
  served_name: {name}
  aliases: []
  context_length: {context}
  max_output_tokens: 8192
  capabilities: [chat]
  # Резервные каналы задаются в config/router.yaml, а не здесь.
  pricing:
    input_per_1m_usd: 0
    output_per_1m_usd: 0
  sglang:
    hf_repo: {hf_repo}
    revision: main
    tensor_parallel: {tp}
    gpus: "{gpus}"
    mem_fraction_static: 0.85
    dtype: bfloat16
    shm_size: 32g
    enable_metrics: true
    env:
      HF_HUB_ENABLE_HF_TRANSFER: "1"
    startup_timeout_s: 2400
    extra_args: []
"""


def cmd_add(args: argparse.Namespace) -> int:
    name = args.name.strip()
    if not NAME_RE.match(name):
        print(f"ОШИБКА: имя должно соответствовать {NAME_RE.pattern}", file=sys.stderr)
        return 1
    target = MODELS_DIR / name / "model.yaml"
    if target.exists():
        print(f"ОШИБКА: {target.relative_to(ROOT)} уже существует", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    title = args.title or name
    target.write_text(
        ADD_TEMPLATE.format(
            name=name,
            title=title.replace('"', "'"),
            context=args.context,
            hf_repo=args.hf_repo,
            tp=args.tp,
            gpus=args.gpus,
        ),
        encoding="utf-8",
        newline="\n",
    )
    print(f"Создан {target.relative_to(ROOT)}")
    print("Дальше: отредактируйте файл, затем выполните  make models  и  make up")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    ok = True
    settings = load_registry_settings()
    models = load_registry()
    print(f"Реестр: {len(models)} моделей, включено {sum(1 for m in models if m.enabled)}")
    print(f"Образ SGLang по умолчанию: {settings.get('image')}")

    env_file = ROOT / ".env"
    if env_file.exists():
        print("OK: .env найден")
    else:
        ok = False
        print("НЕТ: .env отсутствует — выполните  python3 tools/gen_secrets.py  или скопируйте .env.example")

    if shutil.which("docker"):
        print("OK: docker найден")
        try:
            out = subprocess.run(
                ["docker", "compose", "version"], capture_output=True, text=True, timeout=20
            )
            print(("OK: " if out.returncode == 0 else "НЕТ: ") + (out.stdout or out.stderr).strip())
        except Exception as exc:  # noqa: BLE001
            print(f"НЕТ: docker compose не запускается: {exc}")
            ok = False
    else:
        ok = False
        print("НЕТ: docker не найден в PATH")

    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=20,
            )
            print("GPU:")
            for line in out.stdout.strip().splitlines():
                print("  " + line)
            count = len([l for l in out.stdout.strip().splitlines() if l.strip()])
            needed = sum(
                _as_int(m.sglang.get("tensor_parallel"), 1) for m in models if m.enabled and m.kind == "SGLangModel"
            )
            print(f"  требуется GPU-слотов: {needed}, доступно: {count}")
            if needed > count:
                print("  ПРЕДУПРЕЖДЕНИЕ: включённые модели не помещаются в доступные GPU")
        except Exception as exc:  # noqa: BLE001
            print(f"  ПРЕДУПРЕЖДЕНИЕ: nvidia-smi не ответил: {exc}")
    else:
        print("ВНИМАНИЕ: nvidia-smi не найден (это нормально вне GPU-хоста)")

    cache = Path(settings["defaults"].get("hf_cache_dir") or "./data/hf-cache")
    print(f"Кэш весов: {cache} — {'есть' if cache.exists() else 'будет создан'}")

    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmgate",
        description="Реестр моделей LLMGate: валидация и генерация конфигурации.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="показать модели реестра").set_defaults(func=cmd_list)
    sub.add_parser("validate", help="проверить реестр").set_defaults(func=cmd_validate)

    render = sub.add_parser("render", help="сгенерировать compose и конфиг LiteLLM")
    render.add_argument("--check", action="store_true", help="только проверить актуальность файлов")
    render.set_defaults(func=cmd_render)

    add = sub.add_parser("add", help="создать заготовку новой модели")
    add.add_argument("name")
    add.add_argument("--hf-repo", required=True, dest="hf_repo")
    add.add_argument("--tp", type=int, default=1)
    add.add_argument("--gpus", default="")
    add.add_argument("--context", type=int, default=32768)
    add.add_argument("--title", default="")
    add.set_defaults(func=cmd_add)

    sub.add_parser("doctor", help="диагностика окружения").set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RegistryError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
