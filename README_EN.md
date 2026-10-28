# Drone CI Build Sync

[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/docker-ready-brightgreen.svg)](https://www.docker.com/)
[![Tests](https://img.shields.io/badge/tests-50%20passed-success.svg)](tests/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

An asynchronous Python (FastAPI) web service, dashboard, and scheduler that monitors remote Git repositories (GitHub, GitLab, Gitea) and automatically triggers Drone CI builds via its official REST API when new commits are pushed.

> 📖 **[Документация на русском языке (Russian version)](README.md)** | **[Contributing & Code Style](CONTRIBUTING.md)** | **[Production Deployment](docs/DEPLOYMENT.md)**

---

## 🌍 The Problem: Why GitHub Webhooks Do Not Work in Russia

Most modern CI/CD systems rely strictly on the **Push Model**: a developer pushes commits to GitHub $\rightarrow$ GitHub sends an HTTP POST webhook to the company's Drone CI server $\rightarrow$ Drone CI starts the build pipeline.

However, in **Russia and restricted intranet environments**, this model is completely broken:
1. **Network Filtering & Route Blocks:** Due to regulatory filtering, IP routing restrictions, and geopolitical network blockades, GitHub's webhook delivery IP ranges often cannot reach Russian servers and VPS hosting providers.
2. **Corporate Firewalls & NAT:** Many enterprise Drone CI instances reside inside closed private corporate networks, behind carrier-grade NAT (CGNAT), or in air-gapped perimeters without public IP addresses or ingress access.
3. **Delivery Failures & Timeout:** Webhooks sent by GitHub fail with timeouts, connection resets, or SSL handshake drops, resulting in silent build failures and blocked deployment workflows.

### 💡 The Solution: Active Outbound Intermediary (Pull-Proxy)

**Drone CI Build Sync** serves as an active **external intermediary and outbound webhook initiator**:
- Instead of passively waiting for incoming webhook deliveries from GitHub, the service initiates outbound connections from your infrastructure to GitHub using lightweight `git ls-remote` commands.
- Because outbound HTTPS connections to GitHub (`https://github.com/*`) remain fully operational, the service reliably detects commit changes.
- Once a new commit SHA is discovered on tracked branches, the service triggers the Drone CI build internally via Drone's official REST API (`POST /api/repos/{owner}/{repo}/builds?branch={branch}`).

```text
[ GitHub / GitLab / Gitea ]
        ▲
        │  1. Outbound lightweight poll: `git ls-remote --heads` (every N minutes)
        │
[ Drone CI Build Sync (This Service) ]
        │
        │  2. New commit SHA detected!
        ▼  3. Trigger build via REST API: POST /api/repos/{owner}/{name}/builds
[ Drone CI Server ] ────► Pipeline Executes (`trigger.event: custom`)
```

---

## ⚡ Key Features

- **⏰ Worker Schedule by Days of the Week & Time Windows:**
  - Configure allowed operating hours for each day of the week (Monday through Sunday).
  - Timezone support (`zoneinfo`) with automatic local time conversion (e.g. `Europe/Moscow`, `UTC`, `Asia/Yekaterinburg`).
  - Supports standard daytime windows (e.g., `09:00–18:00`), overnight shifts crossing midnight (e.g., `22:00–06:00`), and 24/7 full-day modes (`00:00–00:00`).
  - **Global with Repository Override:** Set a system-wide default schedule in `/schedule` or override it per repository (`Always 24/7`, `Follow Global`, or `Custom Repository Schedule`).
  - Automatic skip: Worker consumes zero network bandwidth and skips polling outside the scheduled hours.
  - Manual bypass: The "Check Now" button always triggers an immediate poll regardless of schedule.

- **🔑 Centralized Credentials Registry (`/credentials`):**
  - Store and manage Git credentials (usernames, Personal Access Tokens) and Drone CI tokens in a single place.
  - Link credentials to multiple repositories; update a token once and all associated projects update instantly.
  - **Encryption At-Rest:** All secrets are encrypted in the database using Fernet (AES-128-CBC + HMAC).

- **🔔 Multi-Channel Notification Subsystem (`/notifications`):**
  - Instant alerts for triggered builds and polling errors.
  - **Telegram:** Send notifications to private chats, channels, or groups via Telegram Bot API.
  - **VKontakte (VK):** Dispatch messages to dialogues or group chats via VK `messages.send` API.
  - **Email (SMTP):** HTML/plaintext emails with TLS/SSL support, authentication, and multiple recipients.
  - **Test Buttons:** One-click "🧪 Test Channel" to verify settings before saving.

- **📡 Live Event Streaming via Server-Sent Events (SSE):**
  - Real-time log monitoring at `/logs` and repository pages without page reloads.
  - Connection status indicator, auto-reconnect, and quick log purge buttons.

- **📊 Prometheus Metrics (`/metrics`):**
  - Exposes Prometheus counters, histograms, and gauges for Grafana monitoring (poll duration, triggered builds, error rates, notification statuses, active repositories).

- **🌐 Comprehensive Branch Tracking:**
  - Track **all branches** dynamically or specify exact names and wildcard patterns (e.g., `main, develop, staging, release/*`).
  - Auto-detection of remote deleted branches with a `deleted on remote` status.

- **🛡️ Race Condition Prevention & Zero Disk Clone:**
  - In-memory `asyncio.Lock` per repository prevents simultaneous duplicate checks.
  - Queries `git ls-remote --heads` in milliseconds without disk space consumption.
  - Credentials in URLs are sanitized (`***:***`) to ensure they never leak in logs, UI, or error messages.

- **📦 Air-Gapped / Intranet Ready:**
  - Tailwind CSS assets are locally vendored in `app/static/css/`, guaranteeing seamless operation inside closed networks without external CDN dependencies.

---

## 🏗️ Architecture

```text
drone-ci-build-sync/
├── app/
│   ├── broadcaster.py      # Real-time SSE (Server-Sent Events) log streaming
│   ├── config.py           # Pydantic Settings & environment config
│   ├── crypto.py           # Fernet (AES-128-CBC + HMAC) secret encryption
│   ├── database.py         # SQLAlchemy engine, SQLite WAL mode, and auto-migrations
│   ├── metrics.py          # Prometheus metrics exporter
│   ├── models.py           # Database models (Repository, Schedule, Credentials, Logs)
│   ├── notifier.py         # Multi-channel notification dispatcher (Telegram, VK, Email)
│   ├── poller.py           # Git ls-remote execution and Drone CI API trigger logic
│   ├── scheduler.py        # APScheduler async background task manager
│   ├── schedule.py         # Weekday working hours & timezone calculation module
│   ├── schemas.py          # Pydantic validation schemas
│   ├── routes/
│   │   ├── web.py          # Jinja2 web dashboard views
│   │   └── api.py          # REST API endpoints (SSE, triggers, schedule, test conns)
│   ├── templates/          # Modern Tailwind CSS HTML templates
│   │   ├── base.html       # Layout template with navigation
│   │   ├── index.html      # Repositories overview with schedule badges
│   │   ├── repo_form.html  # Repository configuration & custom schedule modal
│   │   ├── repo_detail.html# Branch states, commit SHAs, and build history
│   │   ├── schedule.html   # Global worker schedule configuration (7 days)
│   │   ├── credentials.html# Centralized credentials registry
│   │   ├── notifications.html # Telegram, VK, and SMTP email settings
│   │   ├── logs.html       # Full event history with live SSE filters
│   │   └── guide.html      # In-app reference manual & .drone.yml instructions
│   └── static/             # Offline CSS/JS assets (Tailwind CSS)
├── docs/
│   └── DEPLOYMENT.md       # Comprehensive production deployment manual
├── tests/                  # 50 unit and integration tests (pytest & AnyIO)
│   ├── test_app.py
│   ├── test_credentials.py
│   ├── test_crypto.py
│   ├── test_metrics.py
│   ├── test_notifications.py
│   ├── test_poller.py
│   ├── test_schedule.py    # Timezone, day-of-week & window boundary tests
│   └── test_stream.py
├── CONTRIBUTING.md         # Developer guide, code style & PR conventions
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── requirements.txt
├── main.py                 # Application entrypoint
└── README.md               # Russian documentation
```

---

## 🚀 Quick Start

### Option 1: Docker Compose (Recommended)

1. Clone the repository:
   ```bash
   git clone https://github.com/shpion34rus/drone-ci-build-sync.git
   cd drone-ci-build-sync
   ```

2. Start the container:
   ```bash
   docker compose up -d --build
   # or using Makefile:
   # make rebuild
   ```

3. Open the web interface:
   - **Dashboard:** [http://localhost:8080](http://localhost:8080)
   - **REST API Docs:** [http://localhost:8080/api/docs](http://localhost:8080/api/docs)
   - **Prometheus Metrics:** [http://localhost:8080/metrics](http://localhost:8080/metrics)

---

### Option 2: Local Python Environment

**Prerequisites:** Python 3.10+ and `git` command-line utility.

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 3. Launch application
python3 main.py
```

---

## ⚙️ Drone CI Configuration: `.drone.yml`

Because builds are initiated externally via the Drone CI REST API rather than standard git push webhooks, Drone CI assigns the event type **`custom`** to these builds.

Ensure your `.drone.yml` pipeline triggers on the **`custom`** event:

```yaml
kind: pipeline
type: docker
name: default

steps:
  - name: test
    image: python:3.11-slim
    commands:
      - pip install -r requirements.txt
      - pytest

  - name: build
    image: docker:dind
    commands:
      - docker build -t myapp .

trigger:
  event:
    - push
    - custom   # <--- REQUIRED for Drone CI Build Sync!
```

---

## ⏰ Configuring Worker Schedules

To prevent off-hours server load and unnecessary Git queries, configure working hours:

1. Navigate to **Расписание (Schedule)** in the top navigation bar.
2. Toggle **Включить опрос строго по расписанию (Enable schedule restrictions)**.
3. Select your local timezone (e.g., `Europe/Moscow`).
4. Configure working hours for each day of the week, or use one-click presets:
   - ⚡ **Weekdays 09:00–18:00**
   - ⚡ **Weekdays 10:00–19:00**
   - ⚡ **All Days (24/7)**
5. In repository settings, customize the schedule mode:
   - `Global`: Follow the system-wide schedule.
   - `Always 24/7`: Run continuously, ignoring global pauses.
   - `Custom`: Set custom days and hours specific to that repository.

---

## 🧪 Running Tests

The test suite contains **50 automated unit and integration tests** covering scheduling logic, timezone conversions, poller execution, encryption, notifications, and SSE streaming:

```bash
source .venv/bin/activate
python3 -m pytest -v
```

---

## 🛠️ Contributing & Code Style

Please review **[CONTRIBUTING.md](CONTRIBUTING.md)** for detailed instructions on code conventions, PEP 8 standards, and pull request procedures.

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
