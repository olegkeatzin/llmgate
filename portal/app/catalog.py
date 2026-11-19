"""Каталог моделей, сгенерированный tools/llmgate.py."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelInfo:
    name: str
    served_name: str
    title: str
    description: str
    kind: str
    enabled: bool
    capabilities: tuple[str, ...]
    context_length: int
    max_output_tokens: int
    tags: tuple[str, ...]
    fallbacks: tuple[str, ...]
    aliases: tuple[str, ...] = ()

    @property
    def is_embedding(self) -> bool:
        return "embedding" in self.capabilities


class ModelCatalog:
    def __init__(self, models: list[ModelInfo]) -> None:
        self._models = models
        self._by_name = {m.name: m for m in models}
        self._by_served = {m.served_name: m for m in models}

    @classmethod
    def load(cls, path: Path) -> "ModelCatalog":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.warning(
                "Каталог моделей %s не найден. Выполните: python3 tools/llmgate.py render", path
            )
            return cls([])
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Не удалось прочитать каталог моделей %s: %s", path, exc)
            return cls([])

        models: list[ModelInfo] = []
        for item in raw.get("models", []):
            try:
                models.append(
                    ModelInfo(
                        name=str(item["name"]),
                        served_name=str(item.get("served_name") or item["name"]),
                        title=str(item.get("title") or item["name"]),
                        description=str(item.get("description") or ""),
                        kind=str(item.get("kind") or "SGLangModel"),
                        enabled=bool(item.get("enabled", True)),
                        capabilities=tuple(item.get("capabilities") or ("chat",)),
                        context_length=int(item.get("context_length") or 0),
                        max_output_tokens=int(item.get("max_output_tokens") or 0),
                        tags=tuple(item.get("tags") or ()),
                        fallbacks=tuple(item.get("fallbacks") or ()),
                        aliases=tuple(item.get("aliases") or ()),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Пропущена запись каталога: %s (%s)", item, exc)
        return cls(models)

    def all(self) -> list[ModelInfo]:
        return list(self._models)

    def enabled(self) -> list[ModelInfo]:
        return [m for m in self._models if m.enabled]

    def names(self) -> set[str]:
        return set(self._by_name)

    def get(self, name: str) -> ModelInfo | None:
        return self._by_name.get(name) or self._by_served.get(name)

    def served_names(self, names: list[str] | None = None) -> list[str]:
        """Имена для поля models в LiteLLM (served_name), с раскрытием "*"."""
        if names is None or "*" in names:
            return [m.served_name for m in self.enabled()]
        result: list[str] = []
        for name in names:
            info = self.get(name)
            if info is not None and info.enabled and info.served_name not in result:
                result.append(info.served_name)
        return result

    def for_role(self, models: list[str]) -> list[ModelInfo]:
        if "*" in models:
            return self.enabled()
        result = []
        for name in models:
            info = self.get(name)
            if info is not None:
                result.append(info)
        return result
