# Модели SGLang

## Как устроен реестр

Одна модель — один каталог:

```
models/
├── qwen3-32b/model.yaml      # основная модель, tensor parallel на 2 GPU
├── qwen3-8b/model.yaml       # лёгкая (по умолчанию выключена)
├── bge-m3/model.yaml         # эмбеддинги (по умолчанию выключена)
├── llm2-qwen3-32b/model.yaml # внешний резервный эндпоинт (выключен)
├── _template-sglang.yaml     # шпаргалка, генератор её игнорирует
└── _template-remote.yaml
```

Имя каталога = имя модели (латиница в нижнем регистре, цифры, дефис, до 40 символов).
Команда `make models` превращает реестр в три файла:

| Файл | Что внутри | Кто читает |
|---|---|---|
| `docker-compose.models.yml` | контейнеры SGLang, GPU, порты, healthcheck | Docker Compose |
| `config/litellm.generated.yaml` | `model_list` + `fallbacks` | LiteLLM |
| `config/models.generated.json` | каталог для интерфейса портала | портал |

**Сгенерированные файлы руками не правят** — их перезапишет следующая `make models`.
Проверка «не разъехались ли файлы с реестром»: `make check` (это же стоит гонять в CI).

## Поля `model.yaml`

### metadata

| Поле | Обязательно | По умолчанию | Смысл |
|---|---|---|---|
| `name` | да | имя каталога | слаг модели, попадает в имя контейнера |
| `title` | нет | `name` | человекочитаемое имя в портале |
| `description` | нет | — | описание для сотрудников |
| `tags` | нет | `[]` | метки: `chat`, `ru`, `embedding`… |

### spec (общее)

| Поле | По умолчанию | Смысл |
|---|---|---|
| `enabled` | `true` | выключенная модель не попадает ни в compose, ни в LiteLLM |
| `served_name` | `name` | имя, которое клиент передаёт в `"model"` |
| `aliases` | `[]` | дополнительные имена в API (например `qwen3`) |
| `context_length` | `32768` | окно контекста, уходит в `--context-length` |
| `max_output_tokens` | `8192` | ограничение для `max_tokens` на стороне LiteLLM |
| `capabilities` | `[chat]` | `chat`, `embedding`, `vision`, `rerank` |
| `fallbacks` | `[]` | цепочка резерва, см. ниже |
| `pricing.input_per_1m_usd` | `0` | $ за 1M входных токенов (для бюджетов) |
| `pricing.output_per_1m_usd` | `0` | $ за 1M выходных токенов |

### spec.sglang (локальная модель)

| Поле | По умолчанию | Смысл |
|---|---|---|
| `hf_repo` | — | `Qwen/Qwen3-32B`; взаимоисключимо с `model_path` |
| `model_path` | — | путь внутри контейнера (для локальных весов) |
| `local_dir` | — | каталог на хосте, монтируется в `/models/<name>` |
| `revision` | `main` | ветка/тег/коммит Hugging Face |
| `tensor_parallel` | `1` | `--tp`; число GPU на модель |
| `gpus` | все | `"0,1"` — индексы из `nvidia-smi` |
| `port` | `30000` | порт **внутри** контейнера; наружу не публикуется |
| `publish_port` | — | опубликовать порт на хост (только для отладки) |
| `mem_fraction_static` | `0.85` | доля VRAM под веса + KV-кэш |
| `dtype` | `bfloat16` | `auto`, `float16`, `bfloat16` |
| `quantization` | — | `awq`, `fp8`, `w8a8_int8`… |
| `max_running_requests` | — | `--max-running-requests` |
| `shm_size` | `32g` | shared memory контейнера |
| `enable_metrics` | `false` | `--enable-metrics` (Prometheus в SGLang) |
| `trust_remote_code` | `false` | `--trust-remote-code` |
| `startup_timeout_s` | `1800` | `start_period` healthcheck (загрузка весов) |
| `env` | `{}` | доп. переменные окружения |
| `extra_args` | `[]` | любые флаги SGLang как есть |

### spec.remote (эндпоинт на другом сервере)

| Поле | Обязательно | Смысл |
|---|---|---|
| `base_url` | да | `http://llm2.corp.local:30000/v1` |
| `upstream_model` | да | `openai/qwen3-32b` — префикс провайдера LiteLLM |
| `api_key_env` | нет | имя переменной из `.env` (по умолчанию `REMOTE_LLM_API_KEY`) |
| `timeout_s` | нет | таймаут запроса |

## Добавить модель

### Быстро: генератор заготовки

```bash
python3 tools/llmgate.py add qwen2.5-72b \
    --hf-repo Qwen/Qwen2.5-72B-Instruct \
    --tp 2 --gpus 0,1 --context 32768

$EDITOR models/qwen2.5-72b/model.yaml   # проверить память, резервы, включить
make models
make up
make logs-model MODEL=qwen2.5-72b
```

### Вручную

1. `mkdir -p models/my-model`
2. скопировать `models/_template-sglang.yaml` или `_template-remote.yaml` в `models/my-model/model.yaml`
3. поправить `metadata.name`, `hf_repo`, `tensor_parallel`, `gpus`
4. `make models && make up`

Генератор проверит: уникальность имён и алиасов, существование резервов, циклы в цепочках,
количество GPU под `tensor_parallel`, конфликты `publish_port`. При ошибке `make models`
ничего не запишет и покажет список проблем.

### Что происходит при `make up`

- Compose создаёт контейнер `llmgate-sglang-my-model` с монтированием `data/hf-cache`;
- SGLang скачивает веса (если их нет) и грузит на указанные GPU;
- healthcheck опрашивает `/health` каждые 30 с (`start_period` — до 30 минут);
- LiteLLM сразу знает про модель — конфиг читается при его старте.

Пока модель грузится, LiteLLM уже отдаёт её в `/v1/models`, но запросы будут падать
по таймауту подключения — это нормально для первых минут.

## Раскладки на 2×A100

Память: bf16-веса ≈ 2 ГБ на 1 млрд параметров. У Qwen3-32B это ~65 ГБ,
у Qwen3-8B — ~16 ГБ. KV-кэш у 32B — примерно 256 КБ на токен (128 КБ на GPU при `tp=2`),
то есть 32k контекста ≈ 8 ГБ суммарно.

### A. Флагман: одна модель на двух картах (по умолчанию)

```yaml
tensor_parallel: 2
gpus: "0,1"
mem_fraction_static: 0.85
context_length: 32768
```

На 2×A100 80 ГБ — с запасом. На 2×A100 **40 ГБ** — только с `mem_fraction_static: 0.92`
и `context_length: 16384` (веса занимают 32,8 ГБ на карту, остальное — кэш).

### B. Две модели, по одной на карту (нужен 80 ГБ на карту)

```yaml
# qwen3-32b
tensor_parallel: 1
gpus: "0"
mem_fraction_static: 0.90
context_length: 16384
---
# qwen3-8b
tensor_parallel: 1
gpus: "1"
mem_fraction_static: 0.60
```

Плюс: локальный резерв для 32B работает без второй машины.
Минус: контекст меньше, обе модели конкурируют за память при пиках.

### C. Лайт: быстрая модель + эмбеддинги

```yaml
# qwen3-8b:  gpus "0", mem_fraction_static 0.75
# bge-m3:    gpus "1", mem_fraction_static 0.30, capabilities [embedding]
```

Для RAG-сценариев: генерация на одной карте, векторы — на другой.

### D. Внешний резерв

`models/llm2-qwen3-32b/model.yaml`, `kind: RemoteModel`: контейнер не создаётся,
LiteLLM ходит по сети на `base_url`. Включение — `enabled: true` и ключ в `.env`.

## Проверить, что модель жива

```bash
make ps                                  # STATUS: healthy
make logs-model MODEL=qwen3-32b          # загрузка весов, ошибки CUDA/OOM

# health изнутри сети (порт наружу не публикуется)
docker compose exec portal python -c \
  "import urllib.request; print(urllib.request.urlopen('http://sglang-qwen3-32b:30000/health').status)"

# список моделей глазами LiteLLM
curl -s http://localhost:4000/v1/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" | python3 -m json.tool
```

## Резервные каналы

Цепочка задаётся в `fallbacks` основной модели:

```yaml
spec:
  fallbacks: [qwen3-8b, llm2-qwen3-32b]
```

Порядок важен: сначала `qwen3-8b`, потом внешний сервер. LiteLLM сначала повторит
запрос к той же модели (`num_retries: 2`, `cooldown_time: 45` из `config/llmgate.yaml`),
и только потом пойдёт по цепочке.

Что считается отказом: ошибка подключения, таймаут (`timeout: 600` с), HTTP 5xx.
HTTP 4xx (например `context_length_exceeded`) резервом **не** лечится.

Важно:

- резерв должен быть **включён** (`enabled: true`), иначе генератор исключит его из
  `model_list` с предупреждением, и клиент получит `model not found`;
- модель-резерв должна принимать тот же формат запроса (все OpenAI-совместимы);
- при стриминге резерв срабатывает только до первого токена: если поток уже начался,
  переключение невозможно — клиент увидит обрыв.

### Как проверить резерв за две минуты

```bash
# 1. Включить вторую модель и перегенерировать
#    models/qwen3-8b/model.yaml: enabled: true, gpus: "1", mem_fraction_static: 0.45
make models && make up
make ps                                  # оба sglang-* healthy

# 2. Запрос к основной модели
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen3-32b","messages":[{"role":"user","content":"кто ты?"}]}' | head -c 200

# 3. Гасим основную и повторяем
docker compose stop sglang-qwen3-32b

# 4. В логах LiteLLM видно переключение
make logs
```

Ожидаемо: запрос всё равно отвечает, в теле ответа `"model": "qwen3-8b"`,
в логах — `Fallback to qwen3-8b`.

«Последняя надежда» на случай, когда у модели нет своей цепочки:

```yaml
# config/llmgate.yaml
router:
  default_fallbacks: [qwen3-8b]
```

## Офлайн-контур и предзагрузка весов

Веса лежат в `data/hf-cache` (монтируется в `/root/.cache/huggingface`).
Чтобы не тянуть их на боевом сервере:

```bash
# на машине с интернетом, тем же образом, что и на сервере
docker run --rm -v "$PWD/data/hf-cache:/root/.cache/huggingface" \
  -e HF_HUB_ENABLE_HF_TRANSFER=1 \
  lmsysorg/sglang:v0.5.23-cu130 \
  python3 -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-32B')"

rsync -a data/hf-cache/ server:/opt/llmgate/data/hf-cache/
```

Затем в `model.yaml`:

```yaml
spec:
  sglang:
    env:
      HF_HUB_OFFLINE: "1"
      TRANSFORMERS_OFFLINE: "1"
```

Альтернатива для готового каталога весов (без HF-кэша):

```yaml
spec:
  sglang:
    model_path: /models/qwen3-32b
    local_dir: ./data/models/qwen3-32b
```

Для gated-моделей положите `HF_TOKEN` в `.env`.

## Тюнинг

| Симптом | Что менять |
|---|---|
| `CUDA out of memory` при старте | уменьшить `mem_fraction_static` (0.85 → 0.80), `context_length` |
| Модель грузится, но падает на длинных запросах | уменьшить `context_length` или `max_running_requests` |
| Мало параллельных запросов | `mem_fraction_static` вверх (осторожно: сожмёт запас под пики) |
| Не влезает в 40 ГБ | `quantization: awq` + AWQ-веса (например `Qwen/Qwen2.5-32B-Instruct-AWQ`) |
| Медленный старт | веса на NVMe, `HF_HUB_ENABLE_HF_TRANSFER=1` (уже включён) |
| Нужен «размышляющий» Qwen3 | `extra_args: ["--reasoning-parser", "qwen3"]` |

Формула прикидки: `веса_на_GPU = 2 ГБ × (параметры_млрд) / tensor_parallel`,
`KV_на_GPU = 0,128 ГБ × контекст_в_тысячах / tensor_parallel` (для 32B с 8 KV-головами).
Сумма должна быть меньше `mem_fraction_static × VRAM` с запасом 2–3 ГБ.

## Выключить или удалить модель

```bash
# выключить: убрать из LiteLLM и compose
$EDITOR models/qwen3-8b/model.yaml   # enabled: false
make models && make up

# удалить контейнер выключенной модели
docker compose rm -sf sglang-qwen3-8b

# освободить диск (осторожно: веса общие для всех моделей!)
du -sh data/hf-cache
```

Удаление модели из LiteLLM означает для клиентов `400 model not found` — предупредите
пользователей и проверьте, что модель не указана в `fallbacks` у других.

## Шпаргалка: во что превращается YAML

```bash
python3 tools/llmgate.py list        # таблица моделей
python3 tools/llmgate.py validate    # ошибки и предупреждения
python3 tools/llmgate.py render      # перегенерировать
python3 tools/llmgate.py render --check
python3 tools/llmgate.py doctor      # docker, GPU, .env
```
