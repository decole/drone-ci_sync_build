.DEFAULT_GOAL := help

# Переменные
DC ?= docker compose
SERVICE ?= drone-build-sync

.PHONY: help up down down-v start stop restart build rebuild logs logs-app ps status exec shell pull clean test

help: ## Показать справку по доступным командам
	@echo "Drone CI Build Sync - Команды Make:"
	@echo ""
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""

up: ## Запустить контейнеры в фоновом режиме (detached mode)
	$(DC) up -d

down: ## Остановить и удалить контейнеры и сети
	$(DC) down

down-v: ## Остановить и удалить контейнеры, сети и тома (volumes)
	$(DC) down -v

start: ## Запустить существующие остановленные контейнеры
	$(DC) start

stop: ## Приостановить работу контейнеров без их удаления
	$(DC) stop

restart: ## Перезапустить контейнеры
	$(DC) restart

build: ## Собрать Docker-образ приложения
	$(DC) build

rebuild: ## Пересобрать Docker-образ и перезапустить сервис в фоне
	$(DC) up -d --build

logs: ## Просмотр логов контейнеров в реальном времени
	$(DC) logs -f

logs-app: ## Просмотр логов сервиса приложения
	$(DC) logs -f $(SERVICE)

ps: ## Показать статус работающих контейнеров
	$(DC) ps

status: ps ## Алиас для команды ps

exec: ## Открыть командную оболочку (sh) внутри запущенного контейнера
	$(DC) exec $(SERVICE) sh

shell: exec ## Алиас для команды exec

pull: ## Скачать обновленные базовые Docker-образы
	$(DC) pull

clean: ## Остановить контейнеры и удалить все тома (volumes) и висячие контейнеры
	$(DC) down -v --remove-orphans

test: ## Запустить автоматические тесты (pytest)
	python3 -m pytest
