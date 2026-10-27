# Руководство по развертыванию (Deployment Guide)

Данный документ содержит полное руководство по развертыванию сервиса **Drone CI Build Sync** в производственной среде (Production).

---

## Оглавление

1. [Требования к окружению](#1-требования-к-окружению)
2. [Архитектурные особенности рантайма](#2-архитектурные-особенности-рантайма)
3. [Развертывание через Docker Compose (Рекомендуется)](#3-развертывание-через-docker-compose-рекомендуется)
4. [Развертывание через Systemd (Bare-metal / VM)](#4-развертывание-через-systemd-bare-metal--vm)
5. [Настройка Reverse Proxy (Nginx и Caddy)](#5-настройка-reverse-proxy-nginx-и-caddy)
6. [Мониторинг через Prometheus и Grafana](#6-мониторинг-через-prometheus-и-grafana)
7. [Резервное копирование и восстановление](#7-резервное-копирование-и-восстановление)
8. [Переменные окружения](#8-переменные-окружения)

---

## 1. Требования к окружению

- **ОС:** Linux (любой современный дистрибутив: Ubuntu 20.04+, Debian 11+, Alma/Rocky Linux, RHEL).
- **Системные пакеты:**
  - `git` версии 2.25+ (необходим для выполнения `git ls-remote`).
  - `ca-certificates` и `curl`.
  - Docker 24+ и Docker Compose v2 (для контейнерного деплоя) **ИЛИ** Python 3.10+ (для bare-metal).
- **Ресурсы:**
  - **CPU:** от 0.5 vCPU (сервис очень легковесный, опрос `git ls-remote` не клонирует исходный код).
  - **RAM:** от 256 МБ RAM (рекомендуется 512 МБ).
  - **Диск:** от 1 ГБ свободного места (под БД SQLite и журнал логов).

---

## 2. Архитектурные особенности рантайма

> ⚠️ **КРИТИЧЕСКИ ВАЖНО:** Запуск Uvicorn **ОБЯЗАН** осуществляться строго в **1 рабочий процесс (`--workers 1`)**.

### Почему один воркер?
1. **Планировщик фоновых задач (APScheduler):** Работает внутри процесса приложения. При нескольких воркерах каждый воркер запустит свой независимый планировщик, что вызовет многократное дублирование опросов и повторный запуск билдов.
2. **Защита от Race Conditions:** В сервисе реализованы мьютексы `asyncio.Lock` на каждый опрашиваемый репозиторий. Они гарантируют, что повторный запуск опроса (например, по таймеру и по кнопке «Проверить сейчас») не будет выполняться одновременно. В многопроцессном режиме локальные блокировки `asyncio.Lock` не разделяются между процессами.
3. **SQLite WAL-режим:** База данных настроена на параллельное чтение и запись с `PRAGMA journal_mode=WAL` и `PRAGMA busy_timeout=5000`.

---

## 3. Развертывание через Docker Compose (Рекомендуется)

### Шаг 1. Клонирование репозитория
```bash
git clone <URL_РЕПОЗИТОРИЯ> /opt/drone-ci-build-sync
cd /opt/drone-ci-build-sync
```

### Шаг 2. Создание директории данных и назначение прав
```bash
mkdir -p data
```

### Шаг 3. Конфигурация `.env`
Создайте файл `.env` в корне проекта:
```ini
APP_HOST=0.0.0.0
APP_PORT=8080
DATA_DIR=/app/data
DATABASE_URL=sqlite:////app/data/drone_sync.db

# Настройки Drone CI по умолчанию (подставляются при создании новых репозиториев)
DEFAULT_DRONE_SERVER=https://drone.yourcompany.com
DEFAULT_DRONE_TOKEN=your_super_secret_drone_token

# Секретный ключ шифрования секретов (Fernet 32 url-safe base64).
# Если не указан, сервис сгенерирует его автоматически и сохранит в data/.secret_key
SECRET_KEY=

# Тайм-аут выполнения git ls-remote (в секундах)
GIT_TIMEOUT=20
```

### Шаг 4. Проверка `docker-compose.yml`
Убедитесь, что ваш `docker-compose.yml` монтирует `./data:/app/data`:
```yaml
services:
  drone-build-sync:
    build: .
    container_name: drone-ci-build-sync
    restart: unless-stopped
    ports:
      - "127.0.0.1:8080:8080"
    volumes:
      - ./data:/app/data
    env_file:
      - .env
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/metrics"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
```

### Шаг 5. Сборка и запуск контейнера
```bash
docker compose up -d --build
```

Просмотр логов:
```bash
docker compose logs -f drone-build-sync
```

---

## 4. Развертывание через Systemd (Bare-metal / VM)

Если вы не используете Docker, разверните сервис как системный демон Linux.

### Шаг 1. Создание системного пользователя и директорий
```bash
sudo useradd -r -s /bin/false -d /opt/drone-ci-build-sync drone-sync
sudo mkdir -p /opt/drone-ci-build-sync /opt/drone-ci-build-sync/data
sudo chown -R drone-sync:drone-sync /opt/drone-ci-build-sync
```

### Шаг 2. Установка приложения и виртуального окружения
```bash
cd /opt/drone-ci-build-sync
sudo -u drone-sync git clone <URL_РЕПОЗИТОРИЯ> .

# Создание и наполнение venv
sudo -u drone-sync python3 -m venv .venv
sudo -u drone-sync .venv/bin/pip install --upgrade pip
sudo -u drone-sync .venv/bin/pip install -r requirements.txt
```

### Шаг 3. Создание юнита Systemd
Создайте файл `/etc/systemd/system/drone-ci-build-sync.service`:
```ini
[Unit]
Description=Drone CI Build Sync Service
After=network.target

[Service]
Type=simple
User=drone-sync
Group=drone-sync
WorkingDirectory=/opt/drone-ci-build-sync
EnvironmentFile=-/opt/drone-ci-build-sync/.env
ExecStart=/opt/drone-ci-build-sync/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8080 --workers 1
Restart=always
RestartSec=5s

# Ограничения безопасности Linux
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/drone-ci-build-sync/data
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### Шаг 4. Активация и запуск
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now drone-ci-build-sync
sudo systemctl status drone-ci-build-sync
```

---

## 5. Настройка Reverse Proxy (Nginx и Caddy)

Для обеспечения безопасности и SSL-сертификатов (HTTPS) рекомендуется публиковать веб-интерфейс через Reverse Proxy.

### Вариант A: Nginx

Создайте `/etc/nginx/sites-available/drone-sync.conf`:
```nginx
server {
    listen 80;
    server_name sync.drone.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name sync.drone.example.com;

    ssl_certificate /etc/letsencrypt/live/sync.drone.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sync.drone.example.com/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    client_max_body_size 10M;

    # Защита эндпоинта метрик базовой авторизацией (опционально)
    # location /metrics {
    #     auth_basic "Prometheus Metrics";
    #     auth_basic_user_file /etc/nginx/.htpasswd;
    #     proxy_pass http://127.0.0.1:8080;
    # }

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Тайм-ауты проксирования
        proxy_connect_timeout 30s;
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;
    }
}
```

Активация:
```bash
sudo ln -s /etc/nginx/sites-available/drone-sync.conf /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

---

### Вариант B: Caddy (автоматический HTTPS)

Фрагмент `Caddyfile`:
```caddy
sync.drone.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

---

## 6. Мониторинг через Prometheus и Grafana

Сервис поставляет метрики в формате Prometheus на эндпоинте **`/metrics`**.

### Метрики сервиса

| Метрика | Тип | Описание |
| :--- | :--- | :--- |
| `drone_sync_poll_total` | Counter | Количество выполненных циклов опроса (`repo`, `status="success"\|"error"`) |
| `drone_sync_poll_duration_seconds` | Histogram | Длительность операции `git ls-remote` в секундах (`repo`) |
| `drone_sync_poll_errors_total` | Counter | Количество ошибок опроса (`repo`, `error_type`) |
| `drone_sync_builds_triggered_total` | Counter | Количество триггеров билдов Drone CI (`repo`, `branch`) |
| `drone_sync_notifications_sent_total` | Counter | Количество отправленных уведомлений (`channel`, `status`) |
| `drone_sync_repositories_total` | Gauge | Количество репозиториев в базе (`status="active"\|"paused"`) |
| `drone_sync_branches_tracked_total` | Gauge | Общее количество отслеживаемых веток в системе |
| `drone_sync_service_info` | Gauge | Мета-информация о приложении (`version`, `app_name`) |
| Стандартные Python метрики | Counter/Gauge | Использование памяти, CPU, GC, файловые дескрипторы |

### Пример конфигурации `prometheus.yml`:
```yaml
scrape_configs:
  - job_name: "drone-build-sync"
    scrape_interval: 15s
    scrape_timeout: 10s
    static_configs:
      - targets: ["127.0.0.1:8080"]
```

### Рекомендуемые правила алертинга (Prometheus Alerting Rules):
```yaml
groups:
  - name: drone_sync_alerts
    rules:
      - alert: DroneSyncDown
        expr: up{job="drone-build-sync"} == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Drone CI Build Sync сервис недоступен"

      - alert: DroneSyncHighPollErrors
        expr: rate(drone_sync_poll_errors_total[5m]) > 0.1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Повышенная частота ошибок опроса репозиториев в Drone Sync"

      - alert: DroneSyncNotificationFailures
        expr: rate(drone_sync_notifications_sent_total{status="error"}[10m]) > 0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Ошибки отправки нотификаций (Telegram/VK/Email)"
```

---

## 7. Резервное копирование и восстановление

Все данные сервиса содержатся в директории `data/`:
1. `data/drone_sync.db` — база данных SQLite (репозитории, ветки, креденшелы, журнал логов, настройки нотификаций).
2. `data/.secret_key` — ключ симметричного шифрования Fernet (если переменная `SECRET_KEY` не задана в `.env`).

> ⚠️ **ВНИМАНИЕ:** Без файла `.secret_key` (или значения `SECRET_KEY` из `.env`) расшифровать пароли и токены в базе данных **невозможно**. Всегда сохраняйте резервную копию ключа шифрования!

### Горячий бэкап SQLite (безопасный для WAL-режима)
Используйте встроенную команду `.backup` утилиты `sqlite3`:
```bash
sqlite3 /opt/drone-ci-build-sync/data/drone_sync.db ".backup '/backup/drone_sync_$(date +%Y%m%d_%H%M%S).db'"
cp /opt/drone-ci-build-sync/data/.secret_key /backup/.secret_key_$(date +%Y%m%d_%H%M%S)
```

### Восстановление
1. Остановите сервис:
   ```bash
   docker compose down
   # или: sudo systemctl stop drone-ci-build-sync
   ```
2. Скопируйте бэкап БД в `data/drone_sync.db`.
3. Убедитесь в наличии ключа `data/.secret_key` или правильного `SECRET_KEY` в `.env`.
4. Запустите сервис:
   ```bash
   docker compose up -d
   # или: sudo systemctl start drone-ci-build-sync
   ```

---

## 8. Переменные окружения

| Переменная | По умолчанию | Описание |
| :--- | :--- | :--- |
| `APP_NAME` | `Drone CI Build Sync` | Отображаемое имя сервиса |
| `APP_HOST` | `0.0.0.0` | IP-адрес для прослушивания сервером |
| `APP_PORT` | `8080` | Порт веб-приложения |
| `DATA_DIR` | `./data` | Каталог для БД и файлов приложения |
| `DATABASE_URL` | `sqlite:///./data/drone_sync.db` | Строка подключения к базе данных SQLAlchemy |
| `DEFAULT_DRONE_SERVER`| `https://drone.uberserver.ru` | URL Drone CI сервера по умолчанию |
| `DEFAULT_DRONE_TOKEN` | `""` | Токен Drone CI по умолчанию для новых проектов |
| `SECRET_KEY` | `""` | 32 url-safe base64 байта Fernet ключа (опционально) |
| `GIT_TIMEOUT` | `20` | Тайм-аут (сек) вызова `git ls-remote` |
