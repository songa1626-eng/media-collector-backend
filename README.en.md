# MediaCollector Backend

Backend-only Python service for authorized Douyin and Weibo media archiving. It exposes one asynchronous REST API and does not include the private Electron UI.

## Quick start

```bash
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

The combined project is distributed under AGPL-3.0-only. Third-party MIT and Apache-2.0 notices are preserved separately.
