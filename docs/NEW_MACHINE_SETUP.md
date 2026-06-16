# New machine setup (full migration)

Use this checklist when moving **mytbot** to a brand-new PC or reinstalling from scratch. It complements **`README.md`** and mirrors **`requirements.txt`**, **`docker-compose.yml`**, **`config/ai.yaml`**, and **`.env.example`**.

---

## 1. What to copy from the old PC

| Copy | Notes |
|------|--------|
| **Git repository** | Prefer `git clone` (same remote) so history and branches stay intact. If you only zip the folder, copy the whole tree including `.git` **or** re-clone and cherry-pick any local-only commits. |
| **`.env`** | Contains secrets — transfer via a **secure channel** (password manager export, encrypted USB, SFTP). Never commit `.env` to git. If you skip copying, recreate from **`.env.example`** and re-enter keys. |
| **Docker volumes** | Optional. Default DB lives in Docker volume `postgres_data`. For a clean slate on the new PC you usually **do not** copy volumes; run **`alembic upgrade head`** and backfill data later (`run_pipeline.py`). To migrate DB data, use `pg_dump` / `pg_restore` separately. |
| **Ollama models** | Usually **re-pull** on the new machine (`ollama pull …`). Alternatively export/import Ollama’s model blobs if you know how (advanced). |
| **Hugging Face cache** | FinBERT downloads on first use. Optional: copy `%USERPROFILE%\.cache\huggingface` (Windows) or `~/.cache/huggingface` (Linux/macOS) to save bandwidth. |

---

## 2. Install base software

| Tool | Purpose | Notes |
|------|---------|--------|
| **Git** | Clone / pull repo | Any recent version. |
| **Python 3.12+** | Runtime | **64-bit.** 3.13 works per `requirements.txt` pins. Install from [python.org](https://www.python.org/) or your OS package manager; enable **“Add Python to PATH”** on Windows. |
| **Docker Desktop** (Windows/macOS) or **Docker Engine + Compose** (Linux) | Postgres + Redis | Required for local TimescaleDB + Redis (`docker compose up -d`). |
| **Node.js 20 LTS** | Dashboard UI build | Needed for `ui/` (`npm ci`, `npm run build`). |
| **Ollama** | Local LLMs | [ollama.ai](https://ollama.ai) — serves **`http://localhost:11434`** used by **`config/ai.yaml`** (`providers.local_reasoning`). |

**Optional**

- **CUDA GPU**: Faster PyTorch / FinBERT / optional larger models — install NVIDIA drivers + CUDA stack first, then prefer GPU **`torch`** wheels (see **`requirements.txt`** comment for CPU-only index).
- **IBKR TWS / IB Gateway**: Only if you trade via IBKR — enable API; paper typically **7497**, live **7496**.

---

## 3. Clone and Python virtualenv

From an empty folder (or copy your repo tree here):

```bash
git clone https://github.com/emfasys-labs/mytbot.git
cd mytbot
```

Create and activate a venv:

**Windows (PowerShell)**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**GPU vs CPU PyTorch:** `requirements.txt` installs full **`torch`**. On CPU-only machines that is heavier than necessary; you may install CPU wheels instead:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt --no-deps
pip install -r requirements.txt
```

(Adjust if the resolver complains — installing CPU torch first then the rest is the usual pattern.)

**Development / CI parity (optional):**

```bash
pip install -r requirements-dev.txt
```

---

## 4. Environment file (`.env`)

```bash
cp .env.example .env
```

Edit **`.env`** so at minimum:

- **`POSTGRES_*`** match **`docker-compose.yml`** (defaults: `mytbot` / `changeme` / DB name `mytbot`; **`POSTGRES_PORT`** if you avoid port clashes — see comments in **`.env.example`**).
- **`REDIS_*`** — defaults **localhost:6379** if Redis container maps there.
- Broker keys you use (**IBKR** host/port/client id; **Kraken**, **Binance**, etc.).
- **`NEWS_API_KEY`**, **`FRED_API_KEY`** — pipeline/news (`run_pipeline.py`, AI pipeline).
- **`API_CONTROL_TOKEN`** / **`DASHBOARD_READ_TOKEN`** — if you lock down the FastAPI dashboard.

If you **copied `.env`** from the old PC, compare it with **`.env.example`** for any **new** variables added since your last export.

---

## 5. Docker: Postgres + Redis

From repo root (same folder as **`docker-compose.yml`**):

```bash
docker compose up -d
docker compose ps
```

Health: DB container **`mytbot_db`**, Redis **`mytbot_redis`**.

**Windows:** If Python cannot connect but Docker looks fine, see **`.env.example`** — another PostgreSQL service may be bound to port **5432**.

---

## 6. Alembic migrations

Sync DB schema (uses **`POSTGRES_*`** from environment — load `.env` or export vars):

```bash
# Windows venv
.\.venv\Scripts\alembic.exe upgrade head

# macOS/Linux
alembic upgrade head
```

See **`README.md`** for **`alembic stamp head`** if you attach an existing DB created without Alembic.

---

## 7. Local LLMs (Ollama) — align with `config/ai.yaml`

Default **`config/ai.yaml`** expects:

| Setting | Typical value |
|---------|----------------|
| **`providers.local_reasoning.provider`** | `ollama` |
| **`providers.local_reasoning.base_url`** | `http://localhost:11434` |
| **`providers.local_reasoning.model_name`** | `qwen2.5:7b` |
| **`providers.local_reasoning.fallback_model`** | `llama3.1:8b` |

**Install models** (after [Ollama](https://ollama.ai) is installed and running):

```bash
ollama pull qwen2.5:7b
ollama pull llama3.1:8b
```

Verify:

```bash
curl http://localhost:11434/api/tags
```

**CPU-only / slower machines:** In **`config/ai.yaml`**, lower **`providers.local_reasoning.gpu_concurrency`** (e.g. `3` or `1`) and increase **`pipeline.cycle_timeout_seconds`** if the AI cycle times out.

**Remote GPU server:** Point **`base_url`** at an OpenAI-compatible endpoint (e.g. vLLM); keep **`model_name`** consistent with what that server exposes.

---

## 8. FinBERT / Hugging Face (transformers)

**`ProsusAI/finbert`** downloads on first classification pass (~hundreds of MB). Ensure disk space and network.

Optional cache location:

**Windows:** `%USERPROFILE%\.cache\huggingface`  
**Linux/macOS:** `~/.cache/huggingface`

Set **`HF_HOME`** or **`TRANSFORMERS_CACHE`** if you want the cache on a larger disk.

---

## 9. Dashboard UI (`ui/`)

```bash
cd ui
npm ci
npm run build
cd ..
```

Dev server (optional): `npm run dev` — typically **`http://localhost:5173`** with Vite proxy to API per **`ui`** config.

---

## 10. Sanity checks (optional)

```bash
# DB connectivity (from repo root, venv active)
python scripts/verify_db_connection.py

# Tests (pytest reads tests/conftest.py — mitigates DASHBOARD_READ_TOKEN in developer .env)
pytest tests/ -q --tb=line
```

---

## 11. First data & features (optional but typical)

After migrations:

```bash
python run_pipeline.py --backfill
```

Uses **`config/data_pipeline.yaml`** and **`NEWS_API_KEY`** / **`FRED_API_KEY`** when set.

---

## 12. Run the full stack

Single command from repo root:

```bash
python run.py
```

This brings up orchestrator (deps, brokers, trading loop, pipeline task), FastAPI + WebSocket, and serves the built UI when **`ui/dist`** exists.

Stop: **Ctrl+C** (graceful shutdown).

---

## 13. Automated bootstrap scripts

Repo root helpers (venv + pip install; copy `.env` only if missing):

| Script | Platform |
|--------|----------|
| **`scripts/setup_new_machine.ps1`** | Windows PowerShell |
| **`scripts/setup_new_machine.sh`** | macOS / Linux (`chmod +x` first) |

They **do not** install Docker, Node, or Ollama — run sections **2**, **7**, and **9** manually.

---

## 14. Troubleshooting quick reference

| Symptom | Things to check |
|---------|-------------------|
| **`pip install` fails on torch / pydantic** | Python version (3.12+); Visual C++ Build Tools on Windows if building from source (prefer wheels). |
| **Postgres auth / connection refused** | **`docker compose ps`**; **`POSTGRES_*`** in `.env`; port **5432** conflict on Windows. |
| **Ollama connection errors** | `ollama serve` / service running; **`curl localhost:11434`**; **`base_url`** in **`config/ai.yaml`**. |
| **401 on API in tests** | **`PYTEST_API_DISABLE_READ_MIDDLEWARE`** — see **`tests/conftest.py`** / **D023** in **`docs/DECISIONS.md`**. |
| **IBKR Error / no connection** | TWS/Gateway running; API enabled; **`IBKR_PORT`** paper vs live. |

---

## 15. Requirements files reference

| File | Role |
|------|------|
| **`requirements.txt`** | Production/runtime Python deps (`pip install -r requirements.txt`). |
| **`requirements-dev.txt`** | Extends **`requirements.txt`** with **`pytest-cov`** etc. |
| **`requirements-ibkr-test.txt`** | Narrow extras for IBKR-focused tests (see file header). |
| **`docker-compose.yml`** | **`db`** (TimescaleDB), **`redis`** — `.env` drives passwords/ports. |
| **`config/ai.yaml`** | Local-first AI routing (rules, FinBERT, Ollama); not secrets. |
| **`.env.example`** | Full list of env vars — copy to **`.env`**. |

---

After this checklist, **`CLAUDE.md`** “CURRENT STATE” and **`docs/DECISIONS.md`** remain the source for architecture decisions; this doc is **operational onboarding** only.
