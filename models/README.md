# Реестр моделей

Здесь лежат **ваши** модели. Каталог `models/<имя>/model.yaml` превращается в
контейнер SGLang и запись в LiteLLM командой `make models`.

Реестр намеренно пустой: модель, GPU и объём памяти зависят от вашего железа.
Готовые примеры лежат в `examples/models/` и **не загружаются**, пока вы их не скопируете.

## Добавить модель

```bash
# 1. Заготовка с подсказками
python3 tools/llmgate.py add my-model --hf-repo org/model-name --tp 1 --gpus 0

# 2. Или готовый пример (2×A100)
cp -r examples/models/qwen3-32b models/
make models && make up
```

Минимально достаточно четырёх строк — остальное по умолчанию:

```yaml
# models/my-model/model.yaml
kind: SGLangModel
spec:
  sglang:
    hf_repo: org/model
```

Полное описание полей и раскладок — [../docs/02-models.md](../docs/02-models.md).

## Важно

- После добавления модели не забудьте доступ: `config/access.yaml` (если роль не `"*"`).
- `make models` проверяет имена, резервы и GPU и не запишет конфиг с ошибкой.
- Выключить модель без удаления: `enabled: false`.
