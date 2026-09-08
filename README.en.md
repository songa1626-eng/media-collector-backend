# MediaCollector Backend

[![CI](https://github.com/songa1626-eng/media-collector-backend/actions/workflows/ci.yml/badge.svg)](https://github.com/songa1626-eng/media-collector-backend/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-only-blue)](LICENSE)

[中文 README](README.md)

Backend-only Python service for authorized Douyin and Weibo media archiving. It exposes one asynchronous REST API and does not include the private Electron UI.

MediaCollector Backend unifies platform adapters, profile pagination, download jobs, concurrency control, metadata storage, and local media management behind one FastAPI service. It can run independently or power a desktop client, CLI, or custom application.

## Why this project

The project is designed for long-lived archiving workflows rather than one-off URL resolution:

- one REST API for Douyin and Weibo;
- full-history, date-range, page-range, and incremental jobs;
- shared task, retry, concurrency, deduplication, metadata, and storage concepts;
- isolated platform executors that can be extended over time.

## Features

| Area | Capabilities |
| --- | --- |
| Douyin | Single videos, image posts, user posts, likes, mixes, music, and live entry points |
| Weibo | User media, images, videos, expanded image sets, and HLS remuxing |
| Jobs | Async submission, status polling, result statistics, concurrency limits, retries |
| Storage | Local media, metadata, and SQLite download records |
| API | FastAPI, OpenAPI, Swagger UI, optional API key |
| Deployment | Windows, Linux, macOS, and Docker |

`full_history` means the service keeps paginating until a platform boundary or configured limit is reached. It cannot retrieve deleted, private, hidden, unauthorized, or unavailable content.

## Quick start

```bash
git clone https://github.com/songa1626-eng/media-collector-backend.git
cd media-collector-backend

python -m venv .venv
python -m pip install -U pip
pip install -e ".[browser]"
python -m playwright install chromium
cp config.example.yml config.yml
media-collector-api --config config.yml
```

Open `http://127.0.0.1:8000/docs` for interactive API documentation.

Submit a job:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/downloads \
  -H "Content-Type: application/json" \
  -d '{"provider":"weibo","url":"https://weibo.com/u/USER_ID","full_history":true}'
```

Use this software only for content you own, content you are explicitly authorized to archive, or uses otherwise permitted by law. Read [Security and compliance](docs/SECURITY_AND_COMPLIANCE.md) before deployment.

## Architecture

```text
Desktop client / CLI / custom frontend
                  |
                  | REST API
                  v
            FastAPI service
                  |
              JobManager
          jobs, concurrency, state
             /              \\
            /                \\
     Douyin executor     Weibo executor
            |                |
     pagination/download  pagination/download
             \\              /
              \\            /
             local media and metadata
```

The HTTP layer validates requests, handles authentication, schedules jobs, and returns status. Platform executors own platform-specific login state, pagination, and media handling.

## Official desktop edition

This repository contains the backend only. A separate desktop client provides a visual workflow and local media management experience, including:

- Douyin and Weibo download entry points;
- creator profile archiving;
- multi-job queues and progress tracking;
- a local media library and metadata browsing;
- history, filtering, search, and re-download workflows.

The desktop source, assets, and product licensing logic are not included here. Developers can use this API directly or build their own client. The desktop edition may be offered as a separate commercial product with installation packages, updates, private deployment, support, and custom development. Read [Commercial licensing](COMMERCIAL_LICENSE.md) before combining or redistributing the backend.

The combined project is distributed under AGPL-3.0-only. Third-party MIT and Apache-2.0 notices are preserved separately.

AGPL permits commercial use, but modified network services must comply with the applicable source-availability obligations. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and `LICENSES/` for reused components.

## Community

Use GitHub Issues for bugs, deployment questions, and feature requests. A QQ group entry will be added after the group and moderation process are ready. Do not share cookies, tokens, passwords, database files, or other sensitive information in Issues, group chats, or screenshots.
