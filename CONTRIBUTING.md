# Developer & Contributing Guide

Thank you for contributing to **Drone CI Build Sync**! This guide contains coding standards, architectural rules, and instructions for setting up the development environment.

---

## 1. Project Overview & Mission

**Drone CI Build Sync** serves as an active outbound polling mediator (pull-proxy) for triggering Drone CI builds. Direct incoming webhooks from GitHub to CI/CD servers in Russia and restricted intranet environments often fail due to network filtering, geo-blocks, and closed NAT/firewalls. This service queries remote Git repositories via lightweight `git ls-remote` calls and triggers builds via Drone CI's official REST API whenever new commit SHAs are detected.

---

## 2. Development Setup

### Requirements
- **Python**: 3.10, 3.11, 3.12, 3.13, or 3.14.
- **Git**: 2.25+ installed and available in `$PATH`.
- **Operating System**: Linux, macOS, or Windows (WSL recommended).

### Local Environment
```bash
# 1. Clone the repository
git clone https://github.com/shpion34rus/drone-ci-build-sync.git
cd drone-ci-build-sync

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Copy and adjust environment variables (optional)
cp .env.example .env  # Or create .env based on app/config.py

# 5. Run the development server
python3 main.py
```

The web dashboard is available at `http://localhost:8080`, and the OpenAPI Swagger documentation is at `http://localhost:8080/api/docs`.

---

## 3. Architecture & Core Invariants

When developing or refactoring, strictly preserve the following architectural principles:

### A. Single Worker Invariant (`--workers 1`)
- **APScheduler** runs inside the application process (`app/scheduler.py`).
- **Concurrency Locks** (`_repo_locks` in `app/poller.py`) use in-process `asyncio.Lock` to guarantee that duplicate simultaneous polls for the same repository never occur.
- **Running Uvicorn with multiple workers (`--workers > 1`) will cause multiple independent scheduler instances, race conditions, and duplicate builds.** Always run with a single worker process.

### B. SQLite WAL Mode & Concurrency
- SQLite is configured with WAL (Write-Ahead Logging) and `busy_timeout=5000` via events in `app/database.py`.
- Schema changes must be added to `auto_migrate_sqlite` in `app/database.py` to ensure existing deployments upgrade automatically without loss of data or needing complex manual migrations.

### C. Secret Encryption & Zero-Leak Masking
- All passwords, Personal Access Tokens (PATs), Bot Tokens, and SMTP secrets must be encrypted at rest using Fernet (AES-128-CBC + HMAC) via `app/crypto.py`.
- Git URLs and credentials must be masked before writing to `PollLog` or returning in API responses using `mask_git_url()` and `secret_masked`.
- Never log plain credentials, Bearer tokens, or passwords.

### D. Air-Gapped / Intranet Autonomy
- All CSS and JavaScript assets (Tailwind CSS) must remain vendored in `app/static/`.
- The dashboard must function completely offline and in air-gapped environments without external CDN requests.

---

## 4. Code Style & Standards

- **PEP 8**: Follow standard Python conventions.
- **Type Annotations**: Annotate function parameters and return types using standard typing (`Optional`, `List`, `Dict`, `Tuple`, or modern `X | None`, `list[str]`).
- **Pydantic V2**: Use `model_config = ConfigDict(from_attributes=True)` and Pydantic V2 idioms (`model_dump()`).
- **Formatting**:
  - Indentation: 4 spaces (no tabs).
  - Quotes: Double quotes `"` for docstrings and standard strings, or consistent single quotes `'` where appropriate.
  - Line length: Aim for $\le 120$ characters.
- **Naming Conventions**:
  - Modules & packages: `snake_case` (e.g., `scheduler.py`, `broadcaster.py`)
  - Classes: `PascalCase` (e.g., `Repository`, `WorkerScheduleSettings`)
  - Functions & variables: `snake_case` (e.g., `check_schedule_window()`)
  - Constants: `UPPER_SNAKE_CASE` (e.g., `DAY_KEYS`, `DEFAULT_WORKER_SCHEDULE`)
- **Error Handling**:
  - Always clean up database sessions in `finally:` blocks.
  - Catch domain exceptions (`GitError`, `DroneError`) explicitly; do not swallow unexpected exceptions silently.

---

## 5. Testing Guidelines

The test suite is built on **pytest** and **AnyIO** in the `tests/` directory.

### Running Tests
```bash
# Run all tests
python3 -m pytest

# Run with verbose output
python3 -m pytest -v

# Run a specific test module
python3 -m pytest tests/test_schedule.py -v
```

### Writing New Tests
1. **Database Fixtures**: Use `TestingSessionLocal` from `tests/conftest.py` for direct database assertions.
2. **Mocking External Services**:
   - Mock Git network operations (`fetch_remote_heads`) or Drone CI HTTP endpoints when testing edge cases.
   - Do not make external network requests in unit tests.
3. **Async Tests**: Mark async tests with `@pytest.mark.anyio` (not `@pytest.mark.asyncio`, to ensure compatibility with AnyIO).
4. **Coverage**: Ensure every new feature (models, routes, schedules, pollers) is covered by comprehensive unit and integration tests.

---

## 6. Commit Message Guidelines

We follow the **Conventional Commits** specification:

```text
<type>(<scope>): <short description>

[optional body]

[optional footer(s)]
```

### Allowed Types
- `feat`: A new feature or capability (e.g., `feat(schedule): add weekly working hours schedule`).
- `fix`: A bug fix (e.g., `fix(poller): handle timeout in git ls-remote gracefully`).
- `docs`: Documentation updates or additions (e.g., `docs(readme): add English deployment guide`).
- `style`: Changes that do not affect code logic (formatting, whitespace).
- `refactor`: Code restructuring without changing behavior.
- `test`: Adding or correcting tests.
- `chore`: Maintenance tasks, dependency updates, tooling.

---

## 7. Pull Request (PR) Checklist

Before submitting a PR:
- [ ] Virtual environment dependencies are up to date (`pip install -r requirements.txt`).
- [ ] All tests pass without warnings or errors (`python3 -m pytest`).
- [ ] Code follows PEP 8 and project style conventions.
- [ ] Any schema modifications are registered in `app/database.py` (`auto_migrate_sqlite`).
- [ ] Documentation (`README.md`, `DEPLOYMENT.md`, `guide.html`) is updated accordingly.
