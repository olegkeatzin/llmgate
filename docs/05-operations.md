# Эксплуатация

## Что нужно на сервере

| Компонент | Версия | Проверка |
|---|---|---|
| Linux | Ubuntu 22.04+/RHEL 9 | `uname -a` |
| NVIDIA Driver | 550+ | `nvidia-smi` |
| nvidia-container-toolkit | актуальный | `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi` |
| Docker Engine | 24+ | `docker version` |
| Docker Compose | v2 | `docker compose version` |
| Python (на хосте) | 3.11+ + PyYAML | `python3 -c "import yaml"` |

Установка toolkit (Ubuntu):

```bash
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

## Первый запуск

```bash
git clone <репозиторий> /opt/llmgate
cd /opt/llmgate

make env                 # .env со случайными секретами
$EDITOR .env             # LDAP_*, PUBLIC_API_BASE_URL
make doctor              # проверит docker, GPU, .env
make up
make ps                  # ждём healthy у sglang-qwen3-32b (5–20 мин)
```

Если модели ещё не скачаны, первый `make up` займёт время: SGLang тянет ~65 ГБ
в `data/hf-cache`. Прогресс — `make logs-model MODEL=qwen3-32b`.

## TLS и reverse proxy

Портал и LiteLLM отдают HTTP. В проде поставьте их за корпоративный nginx/HAProxy
с сертификатом. Рекомендуются два имени:

```nginx
# API и админ-UI
server {
    listen 443 ssl http2;
    server_name llm.corp.local;

    ssl_certificate     /etc/ssl/certs/llm.crt;
    ssl_certificate_key /etc/ssl/private/llm.key;

    client_max_body_size 32m;

    location / {
        proxy_pass http://127.0.0.1:4000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # стриминг
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}

# Портал
server {
    listen 443 ssl http2;
    server_name keys.corp.local;

    ssl_certificate     /etc/ssl/certs/keys.crt;
    ssl_certificate_key /etc/ssl/private/keys.key;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

После включения TLS:

```dotenv
PUBLIC_API_BASE_URL=https://llm.corp.local/v1
PORTAL_COOKIE_SECURE=true
PORTAL_TRUSTED_PROXIES=172.18.0.1
```

`PORTAL_TRUSTED_PROXIES` — адрес, с которого приходит прокси (виден порталу как
`request.client.host`). Нужен, чтобы блокировка перебора работала по реальному IP.

## Резервное копирование

Копировать нужно три вещи:

```bash
# 1. База LiteLLM (ключи, бюджеты, журнал расходов)
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  > "backup-$(date +%F).sql"

# 2. Конфигурация
tar czf config-$(date +%F).tgz config models .env

# 3. (по желанию) веса — восстанавливаются скачиванием, но долго
#    rsync -a data/hf-cache/ backup:/llmgate/hf-cache/
```

Восстановление:

```bash
cat backup-2026-11-19.sql | docker compose exec -T postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
docker compose up -d
```

Храните `.env` отдельно от бэкапа БД: без `LITELLM_SALT_KEY` ключи в дампе
не проверить (хеши не сопоставить), а без `PORTAL_SESSION_KEY` будут разлогинены
все сотрудники.

## Обновления

Порядок: бэкап → обновление → проверка.

### LiteLLM

```bash
docker compose exec -T postgres pg_dump ... > backup.sql   # обязательно
# в .env: LITELLM_IMAGE=ghcr.io/berriai/litellm:<конкретный-тег>
docker compose pull litellm
docker compose up -d litellm
docker compose logs -f litellm | head -50
```

Миграции схемы LiteLLM выполняет сам при старте. Тег лучше фиксировать
(`main-stable` — плавающий; для продакшена возьмите конкретную версию и укажите её в `.env`).

### Портал

```bash
docker compose build portal && docker compose up -d portal
```

### SGLang

```bash
# models/qwen3-32b/model.yaml: sglang.image: lmsysorg/sglang:<новый-тег>
make models && docker compose pull sglang-qwen3-32b && docker compose up -d sglang-qwen3-32b
```

## Логи и мониторинг

```bash
make ps
make logs                 # portal + litellm
make logs-model MODEL=qwen3-32b
docker compose logs -f postgres
```

Логи ограничены 10 МБ × 3 файла на контейнер (см. `x-logging` в compose).

Что смотреть:

- `event=login.*` в портале — входы, отказы, блокировки;
- `event=key.created/revoked` — выдача ключей;
- `Fallback to ...` в LiteLLM — срабатывание резервов;
- `CUDA out of memory` в SGLang — не хватает памяти.

LiteLLM отдаёт `/health/liveliness` и `/health/readiness`; портал — `/healthz` и `/readyz`.
Оба контейнера имеют healthcheck, `make ps` показывает статус.

Метрики Prometheus: раскомментируйте `callbacks: ["prometheus"]` в
`config/litellm.settings.yaml` и перегенерируйте конфиг, затем скрейпьте
`http://litellm:4000/metrics`. Требуется, чтобы в образе был `prometheus_client`.

## Обслуживание диска

```bash
du -sh data/*                 # postgres, hf-cache
docker system df
docker image prune -f         # старые образы SGLang/LiteLLM
```

`data/hf-cache` — самая большая папка (десятки гигабайт). Её нельзя чистить
на работающем стенде: SGLang перечитает веса только после рестарта.

## Секреты: что можно менять, а что нельзя

| Переменная | Менять можно? | Последствия |
|---|---|---|
| `LITELLM_MASTER_KEY` | да | старые admin-токены перестанут работать; пользовательские ключи — нет |
| `LITELLM_SALT_KEY` | **нет** | все выпущенные ключи станут недействительны |
| `POSTGRES_PASSWORD` | да, с обновлением БД | нужен `ALTER USER` или пересоздание `.env` и рестарт |
| `PORTAL_SESSION_KEY` | да | все сессии портала сбросятся (полезно при инциденте) |
| `LDAP_BIND_PASSWORD` | да | нужен рестарт портала |

## Чек-лист перед вводом в эксплуатацию

- [ ] `.env` с правами 600, не в Git, лежит в бэкапе отдельно.
- [ ] TLS включён, `PORTAL_COOKIE_SECURE=true`, `PUBLIC_API_BASE_URL` — https.
- [ ] Порты 4000 и 8080 закрыты файрволом, доступны только через прокси.
- [ ] Пароль `LITELLM_UI_PASSWORD` сильный: UI показывает все ключи и расходы.
- [ ] `store_prompts_in_spend_logs: false` (приватность) — так и оставлено.
- [ ] LDAP по StartTLS/LDAPS, `LDAP_TLS_VERIFY=true`.
- [ ] Роли в `access.yaml` проверены: `*` не пускает лишних.
- [ ] Бэкап БД настроен по расписанию и проверен восстановлением.
- [ ] `make check` проходит, `make test` зелёный.
- [ ] Мониторинг: `make ps`, алерт на `restarting`/`unhealthy`.
