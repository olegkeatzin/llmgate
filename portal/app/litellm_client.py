"""Клиент management API LiteLLM.

Порталу нужны четыре операции: выпустить ключ, показать ключи
пользователя, отозвать ключ и проверить живость прокси.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class LiteLLMError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LiteLLMClient:
    def __init__(
        self,
        base_url: str,
        master_key: str,
        *,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {master_key}"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generate_key(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/key/generate", json=payload)

    async def delete_key(self, litellm_token: str) -> None:
        await self._request("POST", "/key/delete", json={"keys": [litellm_token]})

    async def list_keys(self, user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            "/key/list",
            params={
                "user_id": user_id,
                "return_full_object": "true",
                "size": str(limit),
                "page": "1",
            },
        )
        keys = data.get("keys") or []
        return [item for item in keys if isinstance(item, dict)]

    async def health(self) -> bool:
        try:
            response = await self._client.get("/health/liveliness")
            return response.status_code < 400
        except httpx.HTTPError:
            return False

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise LiteLLMError("LiteLLM недоступен. Проверьте контейнер litellm.") from exc
        if response.status_code >= 400:
            detail = _detail(response)
            log.warning("LiteLLM %s %s -> %s: %s", method, path, response.status_code, detail)
            raise LiteLLMError(_human(status=response.status_code, detail=detail), status=response.status_code)
        try:
            data = response.json()
        except ValueError as exc:
            raise LiteLLMError("LiteLLM вернул некорректный ответ") from exc
        if not isinstance(data, dict):
            raise LiteLLMError("LiteLLM вернул неожиданный ответ")
        return data


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("error") or body)[:300]
    return str(body)[:300]


def _human(*, status: int, detail: str) -> str:
    if status == 401:
        return "LiteLLM отклонил мастер-ключ (проверьте LITELLM_MASTER_KEY)"
    if status == 404:
        return "Объект не найден в LiteLLM"
    if status == 429:
        return "Превышен лимит запросов LiteLLM, попробуйте позже"
    if status >= 500:
        return "Внутренняя ошибка LiteLLM"
    return detail or f"LiteLLM вернул ошибку {status}"
