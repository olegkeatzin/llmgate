# Архитектура

## Состав стенда

| Контейнер | Образ | Порт | Состояние | Перезапуск |
|---|---|---|---|---|
| `llmgate-postgres` | `postgres:16-alpine` | — (внутренний) | `data/postgres` | `unless-stopped` |
| `llmgate-litellm` | `ghcr.io/berriai/litellm` | 4000 | PostgreSQL | `unless-stopped` |
| `llmgate-portal` | сборка `portal/` | 8080 | нет (cookie) | `unless-stopped` |
| `llmgate-sglang-<модель>` | `lmsysorg/sglang` | — (внутренний) | `data/hf-cache` | `unless-stopped` |

Сеть — стандартная для проекта `llmgate`: `llmgate_default`. Файлы `docker-compose.yml`
и сгенерированный `docker-compose.models.yml` объявляют одно и то же имя проекта,
поэтому контейнеры моделей и LiteLLM оказываются в одной сети без ручной настройки.
LiteLLM обращается к модели по имени сервиса: `http://sglang-qwen3-32b:30000/v1`.

## Потоки

### Вход сотрудника

```
браузер → portal:8080 /login
  portal → LDAP: bind сервисной учёткой, поиск пользователя по sAMAccountName
  portal → LDAP: bind найденным DN с паролем сотрудника   ← проверка пароля
  portal → LDAP: группы (memberOf или поиск по group)
  portal: group → policy (config/access.yaml)
  portal → браузер: зашифрованная cookie сессии (8 ч)
```

Пароль живёт только в памяти процесса на время запроса. В логи не попадает ни пароль,
ни его хеш.

### Выпуск ключа

```
браузер → portal POST /keys (+ CSRF)
  portal → LiteLLM GET /key/list?user_id=<логин>     ← проверка лимита ключей
  portal → LiteLLM POST /key/generate
      { duration: "24h", models: [...], rpm_limit, tpm_limit,
        max_parallel_requests, user_id, key_alias, metadata }
  LiteLLM → PostgreSQL: строка ключа (хранится хеш), лимиты, бюджет
  portal → браузер: ключ показывается один раз
```

Портал не хранит ключ: в LiteLLM остаётся хеш, значение знает только сотрудник.
Список своих ключей портал каждый раз читает из LiteLLM через `/key/list`
(`return_full_object=true`), поэтому второго источника правды нет.

### Запрос к модели

```
приложение → litellm:4000 /v1/chat/completions  (Authorization: Bearer sk-…)
  LiteLLM: проверка ключа, срока, модели, rpm/tpm, бюджета
  LiteLLM → sglang-qwen3-32b:30000 /v1/chat/completions
  при ошибке/таймауте: retry (2), затем fallback-цепочка
  LiteLLM → PostgreSQL: запись в LiteLLM_SpendLogs (без промптов)
```

## Кто чем владеет

| Данные | Владелец | Где лежит |
|---|---|---|
| Ключи, их лимиты, бюджеты | LiteLLM | PostgreSQL |
| Журнал расходов | LiteLLM | PostgreSQL, `LiteLLM_SpendLogs` |
| Роли и доступ к моделям | Git + портал | `config/access.yaml` |
| Реестр моделей и резервы | Git | `models/*/model.yaml` |
| Сессия сотрудника | Портал | cookie (шифр Fernet) |
| Веса моделей | SGLang | `data/hf-cache` |

Ни одна из этих сущностей не дублируется в двух местах — это главное правило стенда.

## Отказоустойчивость

| Что упало | Что происходит |
|---|---|
| SGLang-модель | LiteLLM делает retry, затем уходит в резервную цепочку; без резерва — 500/503 клиенту |
| LiteLLM | Портал показывает ошибку «LiteLLM недоступен», ключи не выдаются; API недоступен |
| PostgreSQL | LiteLLM не стартует/деградирует; портал работает, но ключи не выпускает |
| LDAP | Вход невозможен, уже выданные ключи продолжают работать (они не зависят от LDAP) |
| Портал | API и ключи работают, выдача новых ключей — нет |

Важное следствие: **LDAP не в критическом пути инференса**. Падение каталога не ломает
приложения, которые уже используют ключи.

## Порты и периметр

- `4000` — публичный API + админ-UI LiteLLM. Нужен приложениям; в проде — за TLS-прокси.
- `8080` — портал. Нужен только сотрудникам; в проде — за TLS-прокси.
- `5432` и `30000` наружу не публикуются: БД и SGLang доступны только внутри сети Docker.

Рекомендуемая схема: корпоративный reverse proxy (nginx/HAProxy) терминирует TLS,
`/` ведёт на портал, `/v1` — на LiteLLM. Тогда `PORTAL_COOKIE_SECURE=true`,
а `PORTAL_TRUSTED_PROXIES` содержит адрес прокси.

## Масштабирование

- **Больше GPU** — добавьте модели или поднимите `tensor_parallel`; контейнер на модель.
- **Больше нагрузки** — LiteLLM stateless (состояние в PostgreSQL), можно несколько реплик
  за балансировщиком. Портал тоже stateless, но счётчик неудачных входов в памяти:
  при нескольких репликах он станет приблизительным.
- **Несколько серверов** — опубликуйте SGLang на втором хосте и опишите его как
  `kind: RemoteModel`; резервные цепочки работают между хостами.
