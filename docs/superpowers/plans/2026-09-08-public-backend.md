# MediaCollector Public Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone public backend repository for authorized Douyin and Weibo media downloads without Electron UI.

**Architecture:** A single FastAPI process owns validation and asynchronous jobs. Separate Douyin and Weibo executors reuse the existing download engines and return a shared result envelope.

**Tech Stack:** Python 3.10+, FastAPI, aiohttp, Playwright, pytest, Ruff, Docker

---

### Task 1: Create the backend-only repository

**Files:**
- Create: `pyproject.toml`
- Create: `config.example.yml`
- Create: `.env.example`
- Create: `.gitignore`

- [x] Copy only backend Python packages and tests.
- [x] Exclude Electron, renderer, browser profiles, cookies, databases, downloads, and model weights.
- [x] Define Python 3.10+ package metadata and optional dependency groups.
- [x] Verify package discovery with `python -m build`.

### Task 2: Integrate Weibo as an isolated provider

**Files:**
- Create: `weibo_support/provider.py`
- Create: `weibo_support/downloader.py`
- Create: `weibo_support/models.py`
- Test: `tests/test_weibo_provider.py`
- Test: `tests/test_weibo_downloader.py`

- [x] Move the provider without its UI or task database coupling.
- [x] Retain full-picture expansion, date pagination, browser login fallback, and HLS remux.
- [x] Rewrite imports into the public package namespace.
- [x] Run the focused Weibo tests.

### Task 3: Build the unified API

**Files:**
- Modify: `server/app.py`
- Modify: `server/jobs.py`
- Create: `server/cli.py`
- Test: `tests/test_server.py`

- [x] Add `provider=douyin|weibo` to one request schema.
- [x] Add request-local Douyin configuration.
- [x] Dispatch jobs to the matching platform executor.
- [x] Return provider, shared counts, and platform metadata.
- [x] Keep the legacy singular download endpoint during migration.

### Task 4: Harden runtime defaults

**Files:**
- Modify: `server/app.py`
- Create: `SECURITY.md`
- Create: `docs/SECURITY_AND_COMPLIANCE.md`

- [x] Keep the launcher default at `127.0.0.1`.
- [x] Disable CORS unless explicit origins are configured.
- [x] Add optional `X-API-Key` enforcement.
- [x] Document HTTPS, reverse proxy, rate limits, credential handling, and lawful-use boundaries.

### Task 5: Add public project documentation and licensing

**Files:**
- Create: `README.md`
- Create: `README.en.md`
- Create: `docs/API.md`
- Create: `docs/DEPLOYMENT.md`
- Create: `docs/ARCHITECTURE.md`
- Create: `CONTRIBUTING.md`
- Create: `COMMERCIAL_LICENSE.md`
- Create: `THIRD_PARTY_NOTICES.md`
- Create: `LICENSE`
- Create: `LICENSES/MIT-douyin-downloader.txt`
- Create: `LICENSES/Apache-2.0.txt`

- [x] Provide copyable install, launch, request, and polling examples.
- [x] Explain the private UI boundary and monetization model.
- [x] Preserve upstream notices and explain mixed-license obligations.

### Task 6: Verify and prepare Git history

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/ISSUE_TEMPLATE/bug_report.yml`

- [x] Run `ruff check .`.
- [x] Run `python -m pytest`.
- [x] Run `python -m build` and inspect wheel contents.
- [x] Scan tracked files for credentials, private paths, UI assets, databases, and model weights.
- [x] Initialize Git and create an English initial commit.
