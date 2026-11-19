# Установка и первый запуск

Гайд от чистого сервера до рабочего ключа. Три сценария:

| Железо | Что получится | Время |
|---|---|---|
| 2×A100 (сервер) | полный стенд, qwen3-32b на двух картах | 30 мин + загрузка весов |
| 1 GPU 12–24 ГБ (ПК) | qwen3-8b на одной карте | 20 мин + веса |
| без GPU | LiteLLM + портал + тесты, модели нет | 10 мин |

---

## 0. Коротко

```bash
git clone <адрес> llmgate && cd llmgate     # или из bundle, см. §2
make env                                    # .env со случайными секретами
$EDITOR .env                                # LDAP_* и PUBLIC_API_BASE_URL
make example NAME=qwen3-32b                 # реестр пуст: берём пример (или свою модель)
make up                                     # БД + LiteLLM + портал + модель
make ps                                     # ждать healthy у sglang-qwen3-32b
```

Дальше — вход на `http://<host>:8080` и выпуск ключа.

---

## 1. Что должно быть на машине

| Компонент | Требование | Проверка |
|---|---|---|
| ОС | Linux (Ubuntu 22.04+/RHEL 9) для GPU; Windows/macOS — только без GPU | `uname -a` |
| NVIDIA Driver | 580+ для образа cu130 | `nvidia-smi` |
| nvidia-container-toolkit | актуальный | `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi` |
| Docker Engine | 24+ | `docker version` |
| Docker Compose | v2 | `docker compose version` |
| Python на хосте | 3.11+ и PyYAML (для `tools/`) | `python3 -c "import yaml"` |
| Диск | 150 ГБ: веса 65 ГБ + БД + логи | `df -h` |
| Память | 32 ГБ+ | `free -g` |

Установка toolkit (Ubuntu):

```bash
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### Драйвер и образ SGLang

Образ SGLang собран под конкретную CUDA. Если версия не совпадёт с драйвером,
контейнер упадёт с `CUDA driver version is insufficient`.

| `nvidia-smi` → CUDA Version | Драйвер | Образ |
|---|---|---|
| 13.x | 580+ | `lmsysorg/sglang:v0.5.23-cu130` (по умолчанию) |
| 12.9 | 575–579 | `lmsysorg/sglang:v0.5.19-cu129` |
| 12.0–12.8 | 525–574 | `v0.5.19-cu129` — попробовать (minor compatibility); гарантированно `v0.4.7.post1-cu124` |

CUDA 13 — мажорный скачок: на драйвере 12.4 образ `cu130` не запустится вообще
(`CUDA driver version is insufficient`). Внутри CUDA 12.x действует minor version
compatibility, но новые библиотеки могут требовать свежий драйвер, поэтому `cu129`
на 12.4 — «скорее заработает», а `cu124` — гарантия (это сборка середины 2025 года).

Обновление драйвера до 580 не ломает приложения под CUDA 12.x и не требует
установки CUDA Toolkit на хост: рантайм лежит в контейнере.

Образ задаётся один раз в `.env` (`SGLANG_IMAGE`) или отдельно в
`models/<имя>/model.yaml` → `spec.sglang.image`. Проверить совпадение:
`python3 tools/llmgate.py doctor`.

---

## 2. Как получить код

### Из Git (обычный путь)

```bash
git clone <адрес-репозитория> llmgate
cd llmgate
```

Если репозиторий ещё не создан — см. раздел «Приватный репозиторий» в конце.

### Через bundle (без сети / с флешки)

В репозитории уже лежит `dist/llmgate.bundle` — один файл со всей историей.

```bash
scp dist/llmgate.bundle user@host:/opt/
ssh user@host
cd /opt && git clone llmgate.bundle llmgate && cd llmgate
```

### Архивом

```bash
git archive --format=tar.gz -o llmgate.tar.gz HEAD
scp llmgate.tar.gz user@host:/opt/ && ssh user@host 'cd /opt && tar xzf llmgate.tar.gz -C llmgate'
```

---

## 3. Конфигурация `.env`

```bash
make env            # создаст .env, сгенерирует пароли и ключи, права 600
```

Без `make` (Windows): `python tools/gen_secrets.py`.

Обязательно поправить:

| Переменная | Что писать |
|---|---|
| `PUBLIC_API_BASE_URL` | адрес API **глазами пользователя**: `http://10.0.0.5:4000/v1`. Не `localhost`, если сервер удалённый |
| `LDAP_URI` | `ldap://dc01.corp.local:389` или `ldaps://…:636` |
| `LDAP_BIND_DN`, `LDAP_BIND_PASSWORD` | сервисный аккаунт с правом чтения каталога |
| `LDAP_USER_BASE`, `LDAP_GROUP_BASE` | ветки, где лежат сотрудники и группы |
| `LDAP_USER_FILTER` | для AD по умолчанию подходит: `(&(objectClass=person)(sAMAccountName={username}))` |
| `PORTAL_COOKIE_SECURE` | `false` для теста по HTTP, `true` за HTTPS |

Сгенерированные секреты менять не нужно, но знать про них важно:

- `LITELLM_MASTER_KEY` — админский ключ LiteLLM (API и `/ui`);
- `LITELLM_SALT_KEY` — **никогда не менять после первого запуска**, иначе все
  выпущенные ключи перестанут проверяться;
- `PORTAL_SESSION_KEY` — шифрует cookie сессий; смена = разлогинить всех;
- `LITELLM_UI_PASSWORD` — вход в админ-UI (отдельный от LDAP).

Проверить, что всё на месте:

```bash
python3 tools/gen_secrets.py --check
make doctor
```

---

## 4. LDAP: что должно быть в каталоге

Минимум:

1. **Сервисный аккаунт** (обычная доменная учётка): `CN=svc-llmgate,OU=Service,DC=corp,DC=local`.
2. **Группы доступа**: `LLM-Users`, `LLM-Engineers`, `LLM-Admins`.
3. **Сотрудники** в этих группах.

Полная настройка AD/OpenLDAP — в [03-ldap.md](03-ldap.md). Здесь — проверка:

```bash
make up-core                                  # портал и LiteLLM без моделей
docker compose exec -it portal python -m app.ldapcheck ivanov
```

Скрипт покажет группы и роль. Если «РОЛЬ НЕ НАЙДЕНА» — добавьте группу в
`config/access.yaml` (§7).

Если LDAP ещё нет, портал не запустится (в логах — список проблем конфигурации).
Это сознательный fail-fast: лучше упасть при старте, чем пускать всех подряд.

---

## 5. Поставить модель

«Поставить модель» = описать её в `models/<имя>/model.yaml`, скачать веса и
перезапустить. Ниже — под ваше железо.

### 5.1 Выбрать раскладку

| Железо | Модель | `tensor_parallel` | `gpus` | `mem_fraction_static` | `context_length` |
|---|---|---|---|---|---|
| 2×A100 80 ГБ | `qwen3-32b` | 2 | `"0,1"` | 0.85 | 32768 (по умолчанию) |
| 2×A100 40 ГБ | `qwen3-32b` | 2 | `"0,1"` | 0.92 | 16384 |
| 1 GPU 24 ГБ | `qwen3-8b` | 1 | `"0"` | 0.85 | 16384 |
| 1 GPU 12 ГБ | AWQ 7–8B | 1 | `"0"` | 0.90 | 8192 |

Пример ¤qwen3-32b¤ рассчитан на 2×A100 80 ГБ. Скопируйте его в реестр:
¤make example NAME=qwen3-32b¤ — и можно сразу к 5.3.

### 5.2 Поправить `models/<имя>/model.yaml`

Для 2×A100 40 ГБ:

```yaml
# models/qwen3-32b/model.yaml
spec:
  context_length: 16384
  sglang:
    mem_fraction_static: 0.92
```

Для одной GPU: выключить 32B и включить 8B.

```yaml
# models/qwen3-32b/model.yaml
spec:
  enabled: false

# models/qwen3-8b/model.yaml
spec:
  enabled: true
  context_length: 16384
  sglang:
    tensor_parallel: 1
    gpus: "0"
    mem_fraction_static: 0.85
```

Для 12 ГБ — AWQ-веса (квантование в 4 бита):

```yaml
metadata:
  name: qwen25-7b-awq
spec:
  enabled: true
  context_length: 8192
  capabilities: [chat]
  sglang:
    hf_repo: Qwen/Qwen2.5-7B-Instruct-AWQ
    tensor_parallel: 1
    gpus: "0"
    mem_fraction_static: 0.90
    quantization: awq
```

### 5.3 Скачать веса (необязательно, но ускоряет первый старт)

SGLang сам скачает веса при запуске. Если хотите заранее, тем же образом:

```bash
docker run --rm -v "$PWD/data/hf-cache:/root/.cache/huggingface" \
  -e HF_HUB_ENABLE_HF_TRANSFER=1 \
  lmsysorg/sglang:v0.5.23-cu130 \
  python3 -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-32B')"
```

Для офлайн-контура: скачайте так на машине с интернетом, перенесите
`data/hf-cache` на сервер, добавьте в `model.yaml`:

```yaml
spec:
  sglang:
    env:
      HF_HUB_OFFLINE: "1"
      TRANSFORMERS_OFFLINE: "1"
```

### 5.4 Запустить

```bash
make models          # перегенерировать compose и конфиг LiteLLM
make up              # поднять/обновить контейнеры
make logs-model MODEL=qwen3-32b
```

Ждём в логах `The server is fired up and ready to roll!` и статус `healthy`:

```bash
make ps
```

### 5.5 Проверить

```bash
# health SGLang изнутри сети
docker compose exec litellm python3 -c \
  "import urllib.request; print(urllib.request.urlopen('http://sglang-qwen3-32b:30000/health').status)"

# модель видна LiteLLM
export LITELLM_MASTER_KEY=$(grep '^LITELLM_MASTER_KEY=' .env | cut -d= -f2)
curl -s http://127.0.0.1:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"

# прямой запрос
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen3-32b","messages":[{"role":"user","content":"Привет"}]}'
```

Если `model not found` — вы не выполнили `make models` или LiteLLM не перезапущен
(`make restart`).

---

## 6. Добавление и удаление моделей

### 6.1 Добавить ещё одну

```bash
python3 tools/llmgate.py add qwen2.5-72b \
  --hf-repo Qwen/Qwen2.5-72B-Instruct --tp 2 --gpus 0,1 --context 32768
$EDITOR models/qwen2.5-72b/model.yaml     # память, контекст, enabled
make models && make up
```

Или скопировать готовый пример: `make example NAME=qwen3-32b`.
Минимально достаточно четырёх строк:

```yaml
kind: SGLangModel
spec:
  sglang:
    hf_repo: org/model
```

Подробности и раскладки — [02-models.md](02-models.md).

### 6.2 Удалить модель

Порядок важен: сначала убрать ссылки на модель, потом файл.

1. **Убрать из резервных цепочек** — иначе `make models` упадёт с «резерв не найден»:

   ```yaml
   # config/router.yaml
   fallbacks: []          # было: - qwen3-32b: [my-model]
   ```

2. **Убрать из ролей**, если модели перечислены явно:

   ```yaml
   # config/access.yaml
   models: ["qwen3-32b"]  # было: ["qwen3-32b", "my-model"]
   ```

3. **Выключить или удалить из реестра**:

   ```bash
   # мягко: описание остаётся, VRAM освобождается
   $EDITOR models/my-model/model.yaml     # enabled: false

   # жёстко: удалить файл и каталог
   rm -rf models/my-model
   ```

4. **Перегенерировать и применить**:

   ```bash
   make models
   make up          # --remove-orphans уберёт контейнер удалённой модели
   make restart     # LiteLLM перечитает конфиг
   ```

5. **Проверить**:

   ```bash
   export LITELLM_MASTER_KEY=$(grep '^LITELLM_MASTER_KEY=' .env | cut -d= -f2)
   curl -s http://127.0.0.1:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"
   make ps
   ```

6. **Освободить диск** (необязательно; каталог весов один на модель, но блобы
   могут делиться между ревизиями):

   ```bash
   du -sh data/hf-cache/hub/models--org--name
   rm -rf data/hf-cache/hub/models--org--name
   ```

Что с уже выданными ключами: они продолжат работать для остальных моделей, а запрос
к удалённой вернёт `400 model not found`. Ключи, где была только эта модель, станут
бесполезны до истечения срока — это нормально при сроке 24 часа.

Выключение (`enabled: false`) предпочтительнее удаления, если модель может
понадобиться снова: контейнер и запись в LiteLLM уходят, описание остаётся.

---

## 7. Не забыть: доступ к модели

Ключ выдаётся только на модели, перечисленные в роли. Если у роли `models: ["*"]`
(так настроены `users` и `admins`), новая модель доступна сразу. Если модели
перечислены явно — добавьте новую в список.

```yaml
# config/access.yaml
policies:
  - id: users
    models: ["qwen3-32b", "qwen2.5-72b"]   # добавили новую
```

```bash
make restart-portal
```

---

## 8. Первый ключ

1. Открыть `http://<host>:8080`.
2. Войти доменным логином и паролем.
3. Нажать «Получить ключ на 24 ч».
4. Скопировать `sk-…` — он показывается один раз.

```bash
curl http://<host>:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-ВАШ-КЛЮЧ" -H "Content-Type: application/json" \
  -d '{"model":"qwen3-32b","messages":[{"role":"user","content":"Привет"}]}'
```

Если вход отклонён:

- «Неверный логин или пароль» — bind не прошёл; проверить `LDAP_BIND_DN` и пароль;
- «Нет доступа» — учётка вошла, но её группы нет в `access.yaml`;
- страница не открывается — `docker compose logs portal`.

---

## 9. Резервный канал

Сначала настройка в `config/router.yaml` (формат LiteLLM):

```yaml
fallbacks:
  - qwen3-32b: [qwen3-8b, llm2-qwen3-32b]
```

Дальше нужен хотя бы один живой резерв. Два пути:

### A. Внешний сервер (не занимает VRAM)

```yaml
# models/llm2-qwen3-32b/model.yaml
spec:
  enabled: true
  remote:
    base_url: http://llm2.corp.local:30000/v1
    upstream_model: openai/qwen3-32b
    api_key_env: REMOTE_LLM_API_KEY
```

Ключ — в `.env` (`REMOTE_LLM_API_KEY`), затем `make models && make restart`.

### B. Локально, двумя моделями на двух картах (только 80 ГБ)

```yaml
# qwen3-32b: tensor_parallel: 1, gpus: "0", mem_fraction_static: 0.90, context_length: 16384
# qwen3-8b:  enabled: true, gpus: "1", mem_fraction_static: 0.60
```

```bash
make models && make up && make restart
docker compose stop sglang-qwen3-32b        # имитируем отказ
```

Запрос должен ответить, в логах — `Fallback to qwen3-8b`.

**Важно:** на одной 2×A100 нельзя одновременно держать 32B с `tp=2` и 8B на той же
карте — не хватит VRAM. Либо схема B, либо внешний резерв.

---

## 10. Если GPU нет

Проверить можно всё, кроме инференса:

```bash
make up-core                  # PostgreSQL + LiteLLM + портал
python3 -m pytest             # 39 тестов с поддельными LDAP и LiteLLM

# LiteLLM UI и API
open http://localhost:4000/ui
curl -s http://127.0.0.1:4000/health/liveliness
```

Портал в этом режиме будет перезапускаться, пока не заполнены `LDAP_*` — это нормально
(`docker compose logs portal` покажет, чего не хватает). Для проверки логики входа без
каталога используйте тесты: там LDAP подменён.

---

## 11. Обслуживание

```bash
make ps                  # статусы
make logs                # портал + LiteLLM
make logs-model MODEL=qwen3-32b
make restart             # после правки конфигов LiteLLM/портала
make down                # остановить (данные в ./data остаются)

# бэкап
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" > backup.sql
tar czf config.tgz config models .env
```

Обновления, TLS, бэкапы и секреты — [05-operations.md](05-operations.md).

---

## 12. Чек-лист «стенд готов»

- [ ] `make ps`: postgres, litellm, portal, sglang-* — `healthy`.
- [ ] `curl /v1/models` отдаёт ожидаемые модели.
- [ ] Вход в портал работает, ключ выпускается.
- [ ] `curl` с выпущенным ключом отвечает.
- [ ] `python3 -m pytest` — зелёный.
- [ ] `make check` — сгенерированные файлы актуальны.
- [ ] В `access.yaml` нет лишних `"*"`.
- [ ] `.env` не в Git, права 600.
- [ ] Бэкап БД настроен.

---

## Приватный репозиторий

Локальный репозиторий создан (ветка `main`, тег `v1.0.0`), но **удалённого нет** —
я не могу создать его без доступа к вашему GitHub/GitLab.

### GitHub

```bash
gh auth login
gh repo create llmgate --private --source=. --remote=origin --push
git push origin v1.0.0
```

Без `gh`: создайте пустой приватный репозиторий в веб-интерфейсе и выполните

```bash
git remote add origin git@github.com:<org>/llmgate.git
git push -u origin main --tags
```

### GitLab

```bash
git remote add origin git@gitlab.corp.local:llm/llmgate.git
git push -u origin main --tags
```

Проверка перед push, что секретов нет:

```bash
git status --ignored --short | head
git ls-files | grep -E '(^|/)\.env$' && echo "ОШИБКА: .env в индексе" || echo "OK: .env не отслеживается"
```
