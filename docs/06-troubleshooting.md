# Типовые проблемы

Сначала три команды, которые отвечают на большинство вопросов:

```bash
make ps                 # кто жив, кто перезапускается
make logs               # portal + litellm
make logs-model MODEL=qwen3-32b
```

## Портал

| Симптом | Причина | Что делать |
|---|---|---|
| «Не удалось получить список ключей: LiteLLM недоступен» | контейнер `litellm` не поднялся | `make ps`, `docker compose logs litellm` |
| «LiteLLM отклонил мастер-ключ» | `LITELLM_MASTER_KEY` в `.env` не совпадает с ключом запущенного LiteLLM | сверить `.env`, `docker compose up -d litellm` |
| «У вашей учётной записи нет доступа» | нет роли в `access.yaml` | `docker compose exec -it portal python -m app.ldapcheck <логин>` |
| «Сервер LDAP недоступен» | DNS/файрвол/неверный URI | `docker compose exec portal getent hosts <dc>`, проверить 389/636 |
| «Форма устарела…» (403) | страница открыта давно, сессия истекла | обновить страницу, войти заново |
| Вход «успешен», но снова логин | cookie не ставится | при HTTPS должен быть `PORTAL_COOKIE_SECURE=true`, при HTTP — `false` |
| Портал перезапускается каждые 30 с | неверная конфигурация (битый `PORTAL_SESSION_KEY`) | `docker compose logs portal` — там список проблем |
| «Не удалось выпустить ключ: … model not found» | модель выключена или не попала в конфиг | `python3 tools/llmgate.py list`, `make models`, рестарт LiteLLM |

Проверить конфигурацию портала, не заходя в браузер:

```bash
docker compose exec portal python -c \
  "from app.settings import Settings; print(Settings.from_env().ldap.uri)"
```

## LiteLLM

| Симптом | Причина | Что делать |
|---|---|---|
| Контейнер в `Restarting` | нет связи с PostgreSQL или битый конфиг | `docker compose logs litellm \| tail -50` |
| `401 Invalid proxy server token` | ключ не от LiteLLM или истёк | выпустить новый в портале |
| `400 model not found` | модели нет в `model_list` | `make models` + рестарт LiteLLM |
| Все ключи «сломались» после обновления | сменился `LITELLM_SALT_KEY` | вернуть прежнюю соль из бэкапа `.env` |
| `429` на всех запросах | исчерпан `rpm`/`tpm` роли | поднять лимиты в `access.yaml` и выпустить новый ключ |
| В `/ui` пусто | `store_model_in_db`/БД | `docker compose logs litellm` |

Проверка без приложения:

```bash
curl -s http://localhost:4000/health/liveliness
curl -s http://localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

## SGLang и GPU

| Симптом | Причина | Что делать |
|---|---|---|
| `CUDA out of memory` | `mem_fraction_static` + контекст не влезают | уменьшить `mem_fraction_static` на 0,05–0,1 или `context_length` |
| `no CUDA-capable device` | контейнер не видит GPU | `nvidia-ctk runtime configure`, `device_ids` в `model.yaml` |
| Модель качается и падает | gated-репозиторий | `HF_TOKEN` в `.env`, рестарт |
| `401` при скачивании | нет доступа к репозиторию | проверить лицензию на HF |
| Healthcheck `unhealthy`, но модель отвечает | таймаут healthcheck | увеличить `startup_timeout_s` |
| Долгий старт (>30 мин) | медленный диск/сеть | веса на NVMe, `HF_HUB_ENABLE_HF_TRANSFER=1` |
| `NCCL` ошибки при `tp=2` | нет `ipc: host` или `shm` | генератор уже ставит `ipc: host` и `shm_size`; проверьте, что compose не переопределён |

Проверить, что видят контейнеры:

```bash
docker compose exec sglang-qwen3-32b nvidia-smi
docker compose exec portal python -c \
  "import urllib.request; print(urllib.request.urlopen('http://sglang-qwen3-32b:30000/health').status)"
```

## Резервные каналы

| Симптом | Причина | Что делать |
|---|---|---|
| Резерв не срабатывает | модель-резерв выключена | `enabled: true`, `make models`, `make up` |
| Резерв в цепочке, но клиент получает ошибку | резерв недоступен | `docker compose ps`, `make logs` |
| Резерв сработал на 4xx | так не бывает: 4xx не лечится резервом | проверить формат запроса |
| Стриминг оборвался | резерв не подхватывает уже начатый поток | повторить запрос |

## Compose

| Симптом | Причина | Что делать |
|---|---|---|
| `service "sglang-..." depends on undefined service` | запущен только `docker-compose.models.yml` без основного файла | всегда `make up` (оба файла) |
| `port is already allocated` | занят 4000/8080 | поменять `LITELLM_PORT`/`PORTAL_PORT` в `.env` |
| Контейнеры не видят друг друга | разные имена проектов | оба compose-файла должны иметь `name: llmgate` |

## «Ключ не работает в приложении»

Проверьте по порядку:

1. `base_url` оканчивается на `/v1` (например `http://host:4000/v1`).
2. Ключ начинается с `sk-` и не истёк (24 ч).
3. `model` — из `/v1/models` (например `qwen3-32b` или алиас `qwen3`).
4. Ошибка 429 — лимиты `rpm`/`tpm`; 401 — ключ отозван или истёк.

Быстрая проверка:

```bash
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-ВАШ-КЛЮЧ" -H "Content-Type: application/json" \
  -d '{"model":"qwen3-32b","messages":[{"role":"user","content":"ping"}]}'
```

## Ошибки генератора конфигурации

`make models` падает — читайте сообщение, там указан файл и поле:

| Сообщение | Что значит |
|---|---|
| `резерв 'x' не найден в реестре` | опечатка в `config/router.yaml` |
| `резерв 'x' выключен ... будет исключён` | `enabled: false` у цели резерва |
| `цикл в резервных цепочках` | A→B→A |
| `tensor_parallel=2, но перечислено GPU: 1` | в `gpus` указана одна карта |
| `нет ни одной включённой модели` | реестр пуст или все `enabled: false`: `make example NAME=…` |
| `Реестр моделей пуст — генерировать нечего` | нет ни одного `models/<имя>/model.yaml` |
