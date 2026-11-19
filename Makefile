# LLMGate — повседневные команды.
# Требуется GNU make, python3 c PyYAML и Docker Compose v2.

SHELL := /bin/sh
PY ?= python3
COMPOSE := docker compose -f docker-compose.yml -f docker-compose.models.yml

.DEFAULT_GOAL := help
.PHONY: help env models check config up up-core down restart restart-portal ps logs logs-model test doctor psql

help: ## Показать список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.env: ## (служебная цель) создать .env, если его нет
	$(PY) tools/gen_secrets.py

env: ## Сгенерировать .env со случайными секретами (перезапись: make env FORCE=1)
	$(PY) tools/gen_secrets.py $(if $(FORCE),--force,)

models: ## Перегенерировать docker-compose.models.yml и config/litellm.generated.yaml
	$(PY) tools/llmgate.py render

check: ## Проверить реестр моделей и актуальность сгенерированных файлов
	$(PY) tools/llmgate.py validate
	$(PY) tools/llmgate.py render --check

config: ## Проверить синтаксис compose-файлов
	$(COMPOSE) config -q && echo 'OK: compose валиден'

up: .env models ## Поднять весь стек: модели SGLang + LiteLLM + портал
	$(COMPOSE) up -d
	@echo "Готово. Порты портала и API — PORTAL_PORT и LITELLM_PORT в .env"

up-core: .env ## Поднять только LiteLLM + портал (без моделей)
	docker compose up -d

down: ## Остановить и удалить контейнеры (данные остаются)
	$(COMPOSE) down

restart: ## Перезапустить портал и LiteLLM (после правки конфигов)
	$(COMPOSE) restart portal litellm

restart-portal: ## Перезапустить только портал (после правки config/access.yaml)
	$(COMPOSE) restart portal

ps: ## Статус контейнеров
	$(COMPOSE) ps

logs: ## Логи портала и LiteLLM
	$(COMPOSE) logs -f --tail=200 portal litellm

logs-model: ## Логи конкретной модели: make logs-model MODEL=qwen3-32b
	@test -n "$(MODEL)" || (echo 'Укажите MODEL=<имя модели>'; exit 1)
	$(COMPOSE) logs -f --tail=200 sglang-$(MODEL)

test: ## Прогнать тесты (без Docker и сети)
	$(PY) -m pytest

doctor: ## Диагностика: docker, GPU, .env, реестр моделей
	$(PY) tools/llmgate.py doctor

psql: ## Психологически удобный доступ к БД LiteLLM
	$(COMPOSE) exec postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'
