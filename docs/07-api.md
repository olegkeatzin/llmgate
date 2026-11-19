# Работа с API

API полностью совместим с OpenAI: тот же путь `/v1/chat/completions`,
тот же формат тела, поддержка SDK, LangChain, LlamaIndex и прочих клиентов.

## Подключение

| Параметр | Значение |
|---|---|
| Base URL | `https://llm.corp.local/v1` (из `PUBLIC_API_BASE_URL`) |
| Ключ | `sk-…` из личного кабинета, живёт 24 часа |
| Модели | `GET /v1/models` |

## Список моделей

```bash
curl https://llm.corp.local/v1/models \
  -H "Authorization: Bearer $LLM_KEY"
```

```json
{
  "object": "list",
  "data": [
    {"id": "qwen3-32b", "object": "model", "owned_by": "llmgate"},
    {"id": "qwen3", "object": "model", "owned_by": "llmgate"}
  ]
}
```

`qwen3` — алиас `qwen3-32b`; оба имени равнозначны.

## Чат

```bash
curl https://llm.corp.local/v1/chat/completions \
  -H "Authorization: Bearer $LLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-32b",
    "messages": [
      {"role": "system", "content": "Ты — ассистент технической поддержки."},
      {"role": "user", "content": "Как сбросить пароль?"}
    ],
    "temperature": 0.2,
    "max_tokens": 512
  }'
```

Python (официальный SDK):

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="https://llm.corp.local/v1",
    api_key=os.environ["LLM_KEY"],
)

response = client.chat.completions.create(
    model="qwen3-32b",
    messages=[{"role": "user", "content": "Привет"}],
    temperature=0.2,
    max_tokens=512,
)
print(response.choices[0].message.content)
print(response.usage)          # prompt/completion/total tokens
```

## Стриминг

```python
stream = client.chat.completions.create(
    model="qwen3-32b",
    messages=[{"role": "user", "content": "Расскажи про A100"}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
```

Через `curl` добавьте `"stream": true`; прокси не буферизует ответ (в nginx
нужен `proxy_buffering off`, пример — в docs/05-operations.md).

## Эмбеддинги

Если в `access.yaml` роли разрешена `bge-m3` и модель включена:

```python
result = client.embeddings.create(
    model="bge-m3",
    input=["первый документ", "второй документ"],
)
vector = result.data[0].embedding   # 1024 измерения
```

Для RAG обычно: `bge-m3` на отдельной GPU, генерация — на `qwen3-32b`.

## LangChain

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="qwen3-32b",
    base_url="https://llm.corp.local/v1",
    api_key=os.environ["LLM_KEY"],
    temperature=0.2,
)
```

## Лимиты и ошибки

| Код | Что значит | Что делать |
|---|---|---|
| `401 Unauthorized` | ключ неверный, отозван или истёк | выпустить новый в портале |
| `403 Forbidden` | ключ не даёт доступа к модели | проверить role `models` |
| `404 Not Found` | неверный путь (забыли `/v1`) | исправить base URL |
| `429 Too Many Requests` | `rpm`/`tpm` или параллельные запросы | подождать, уменьшить параллелизм |
| `400 context_length_exceeded` | промпт больше `context_length` | сократить, саммаризировать |
| `500/503` | модель недоступна, резерв не сработал | смотреть `make logs` |
| `400 model not found` | модель выключена | `python3 tools/llmgate.py list` |

Лимиты применяются **на ключ**, а не на пользователя: два ключа — два независимых
бюджета `rpm`. `max_parallel_requests` ограничивает одновременные запросы.

## Резервные каналы глазами клиента

Переключение прозрачно: клиент получает ответ, но в поле `model` может быть
резервная модель. Если различие важно (например, для логирования), смотрите
`response.model` и заголовки LiteLLM в логах.

## Идентификация пользователя

Каждый ключ создаётся с `user_id = доменный логин` и `metadata.portal_policy`.
В журнале расходов LiteLLM это видно как `user` и `metadata`; по ним строятся
отчёты «кто сколько потратил».

## Время жизни ключа

24 часа по умолчанию. Приложение должно уметь получать новый ключ; для сервисных
учёток заведите отдельную роль с длинным `key_ttl_hours` (см. docs/04-access.md).
