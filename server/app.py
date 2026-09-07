"""Unified FastAPI entry point for Douyin and Weibo downloads.

The HTTP layer owns job scheduling and input validation. Platform-specific
collectors remain isolated so cookies, pagination, and cleanup do not leak.
"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from auth import CookieManager
from config import ConfigLoader
from control import QueueManager, RateLimiter, RetryHandler
from core import DouyinAPIClient, DownloaderFactory, URLParser
from server.jobs import JobManager
from storage import FileManager
from utils.logger import setup_logger
from utils.validators import is_short_url, normalize_short_url
from weibo_support import AsyncDownloader, WeiboProvider

logger = setup_logger("REST")
DOUYIN_MODES = {"post", "like", "mix", "music", "collect", "collectmix"}


class DownloadRequest(BaseModel):
    provider: Literal["douyin", "weibo"] = "douyin"
    url: str
    modes: List[str] = Field(default_factory=lambda: ["post"])
    save_dir: str = ""
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    start_page: int = Field(default=1, ge=1)
    end_page: int = Field(default=0, ge=0)
    full_history: bool = True
    download_pinned: bool = False


class JobResponse(BaseModel):
    job_id: str
    provider: str
    status: str
    url: str


class _ServerDeps:
    """Resources shared for one API process and closed during shutdown."""

    def __init__(self, config: ConfigLoader):
        self.config = config
        if config.config_path:
            cookie_file = str(Path(config.config_path).resolve().parent / ".cookies.json")
        else:
            cookie_file = ".cookies.json"
        self.cookie_manager = CookieManager(cookie_file=cookie_file)
        initial_cookies = config.get_cookies()
        if initial_cookies:
            self.cookie_manager.set_cookies(initial_cookies)
        else:
            self.cookie_manager.get_cookies()
        self.file_manager = FileManager(config.get("path"))
        self.rate_limiter = RateLimiter(max_per_second=float(config.get("rate_limit", 2) or 2))
        self.retry_handler = RetryHandler(max_retries=int(config.get("retry_times", 3) or 3))
        self.queue_manager = QueueManager(max_workers=int(config.get("thread", 5) or 5))
        self.weibo_provider = WeiboProvider()
        env_cookie = os.getenv("WEIBO_COOKIE", "").strip()
        if env_cookie:
            self.weibo_provider.set_cookie(env_cookie)
        self.weibo_downloader = AsyncDownloader(
            max_concurrent=int(config.get("thread", 5) or 5)
        )

    async def close(self) -> None:
        await self.weibo_downloader.close()
        await self.weibo_provider.close()


def _job_config(base: ConfigLoader, options: Dict[str, Any]) -> ConfigLoader:
    """Create a request-local config so concurrent jobs never mutate globals."""
    config = ConfigLoader(None)
    config.config = deepcopy(base.config)
    updates: Dict[str, Any] = {
        "mode": options.get("modes") or ["post"],
        "download_pinned": bool(options.get("download_pinned", False)),
    }
    if options.get("save_dir"):
        updates["path"] = str(_resolve_output_root(base, options["save_dir"]))
    if options.get("start_date"):
        updates["start_time"] = str(options["start_date"])
    if options.get("end_date"):
        updates["end_time"] = str(options["end_date"])
    config.update(**updates)
    return config


def _resolve_output_root(base: ConfigLoader, requested: str = "") -> Path:
    configured = Path(str(base.get("path") or "./Downloaded")).expanduser().resolve()
    if not requested:
        return configured
    candidate = Path(requested).expanduser().resolve()
    allow_custom = os.getenv("MEDIA_COLLECTOR_ALLOW_CUSTOM_SAVE_DIRS", "").strip() == "1"
    if not allow_custom and candidate != configured and configured not in candidate.parents:
        raise ValueError(
            "save_dir must stay inside the configured download root; "
            "set MEDIA_COLLECTOR_ALLOW_CUSTOM_SAVE_DIRS=1 only for trusted clients"
        )
    return candidate


async def _execute_douyin_download(
    url: str,
    deps: _ServerDeps,
    options: Dict[str, Any],
) -> Dict[str, Any]:
    config = _job_config(deps.config, options)
    file_manager = FileManager(config.get("path"))
    async with DouyinAPIClient(
        deps.cookie_manager.get_cookies(),
        proxy=config.get("proxy"),
    ) as api_client:
        if is_short_url(url):
            resolved = await api_client.resolve_short_url(normalize_short_url(url))
            if not resolved:
                raise RuntimeError(f"Failed to resolve short URL: {url}")
            url = resolved

        parsed = URLParser.parse(url)
        if not parsed:
            raise RuntimeError(f"Unsupported Douyin URL: {url}")

        downloader = DownloaderFactory.create(
            parsed["type"],
            config,
            api_client,
            file_manager,
            deps.cookie_manager,
            None,
            deps.rate_limiter,
            deps.retry_handler,
            deps.queue_manager,
            progress_reporter=None,
        )
        if downloader is None:
            raise RuntimeError(f"No downloader for url_type={parsed['type']}")

        result = await downloader.download(parsed)
        return {
            "total": result.total,
            "success": result.success,
            "failed": result.failed,
            "skipped": result.skipped,
            "filtered": result.filtered,
            "source_total": result.source_total,
            "source_collected": result.source_collected,
            "unavailable_count": result.unavailable_count,
            "unavailable_reason": result.unavailable_reason,
            "warning": result.warning,
        }


def _coerce_date(value: Any) -> Optional[date]:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _validate_platform_url(provider: str, value: str) -> None:
    candidate = value.strip()
    if provider == "weibo" and candidate.isdigit():
        return
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    allowed = {
        "douyin": ("douyin.com", "iesdouyin.com"),
        "weibo": ("weibo.com", "weibo.cn"),
    }[provider]
    if not host or not any(host == suffix or host.endswith(f".{suffix}") for suffix in allowed):
        raise ValueError(f"URL does not belong to {provider}")


async def _execute_weibo_download(
    url: str,
    deps: _ServerDeps,
    options: Dict[str, Any],
) -> Dict[str, Any]:
    target = await deps.weibo_provider.parse_target(url)
    output_root = _resolve_output_root(deps.config, str(options.get("save_dir") or ""))
    save_dir = output_root / "weibo" / target.display_name
    start_page = max(1, int(options.get("start_page") or 1))
    end_page = max(0, int(options.get("end_page") or 0))
    if not options.get("full_history", True) and end_page == 0:
        end_page = start_page

    total = 0
    success = 0
    failed = 0
    seen_ids: set[str] = set()
    headers = deps.weibo_provider.get_download_headers()
    async for _, items in deps.weibo_provider.iter_media_pages(
        target,
        start_page=start_page,
        end_page=end_page,
        start_date=_coerce_date(options.get("start_date")),
        end_date=_coerce_date(options.get("end_date")),
    ):
        pending = []
        for item in items:
            if item.source_id and item.source_id in seen_ids:
                continue
            if item.source_id:
                seen_ids.add(item.source_id)
            pending.append(item)
        if not pending:
            continue
        page_success, page_failed, _ = await deps.weibo_downloader.download_batch(
            pending,
            save_dir,
            headers=headers,
        )
        total += len(pending)
        success += page_success
        failed += page_failed

    return {
        "total": total,
        "success": success,
        "failed": failed,
        "skipped": 0,
        "target_name": target.display_name,
        "save_dir": str(save_dir),
    }


def build_app(config: ConfigLoader) -> FastAPI:
    deps = _ServerDeps(config)

    async def executor(url: str, options: Dict[str, Any]) -> Dict[str, Any]:
        provider = str(options.pop("provider", "douyin"))
        if provider == "douyin":
            return await _execute_douyin_download(url, deps, options)
        if provider == "weibo":
            return await _execute_weibo_download(url, deps, options)
        raise ValueError(f"Unsupported provider: {provider}")

    server_cfg = config.get("server") or {}
    if not isinstance(server_cfg, dict):
        server_cfg = {}
    manager = JobManager(
        executor=executor,
        max_concurrency=int(config.get("thread", 2) or 2),
        max_jobs=int(server_cfg.get("max_jobs") or JobManager.DEFAULT_MAX_JOBS),
        job_ttl_seconds=float(
            server_cfg.get("job_ttl_seconds") or JobManager.DEFAULT_JOB_TTL_SECONDS
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await manager.shutdown()
        await deps.close()

    app = FastAPI(
        title="MediaCollector Backend",
        version="1.0.0",
        description="Backend-only REST API for authorized Douyin and Weibo media archiving.",
        lifespan=lifespan,
    )
    app.state.job_manager = manager
    app.state.deps = deps

    origins = [
        value.strip()
        for value in os.getenv("MEDIA_COLLECTOR_CORS_ORIGINS", "").split(",")
        if value.strip()
    ]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-API-Key"],
        )

    api_key = os.getenv("MEDIA_COLLECTOR_API_KEY", "").strip()
    public_api_paths = {"/api/v1/health", "/api/v1/about"}

    @app.middleware("http")
    async def require_api_key(request: Request, call_next):
        if (
            api_key
            and request.url.path.startswith("/api/")
            and request.url.path not in public_api_paths
            and not hmac.compare_digest(request.headers.get("X-API-Key", ""), api_key)
        ):
            return JSONResponse(status_code=401, content={"detail": "invalid API key"})
        return await call_next(request)

    @app.get("/api/v1/health")
    async def health() -> Dict[str, Any]:
        return {"status": "ok", "version": "1.0.0", "providers": ["douyin", "weibo"]}

    @app.get("/api/v1/about")
    async def about() -> Dict[str, str]:
        return {
            "name": "MediaCollector Backend",
            "version": "1.0.0",
            "license": "AGPL-3.0-only",
            "source_url": os.getenv("MEDIA_COLLECTOR_SOURCE_URL", "").strip(),
        }

    async def submit(req: DownloadRequest) -> JobResponse:
        if not req.url.strip():
            raise HTTPException(status_code=400, detail="url is required")
        if req.start_date and req.end_date and req.start_date > req.end_date:
            raise HTTPException(status_code=400, detail="end_date cannot be earlier than start_date")
        try:
            _validate_platform_url(req.provider, req.url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        invalid_modes = set(req.modes) - DOUYIN_MODES
        if req.provider == "douyin" and invalid_modes:
            invalid = ", ".join(sorted(invalid_modes))
            raise HTTPException(status_code=400, detail=f"unsupported Douyin modes: {invalid}")
        options = req.model_dump(exclude={"url"}, mode="json")
        options["provider"] = req.provider
        job = await manager.submit(
            req.url,
            provider=req.provider,
            options=options,
        )
        return JobResponse(
            job_id=job.job_id,
            provider=job.provider,
            status=job.status,
            url=job.url,
        )

    @app.post("/api/v1/downloads", response_model=JobResponse)
    async def create_download(req: DownloadRequest) -> JobResponse:
        return await submit(req)

    @app.post("/api/v1/download", response_model=JobResponse, deprecated=True)
    async def create_legacy_download(req: DownloadRequest) -> JobResponse:
        return await submit(req)

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> Dict[str, Any]:
        job = await manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job.to_dict()

    @app.get("/api/v1/jobs")
    async def list_jobs() -> Dict[str, List[Dict[str, Any]]]:
        jobs = await manager.list_jobs()
        return {"jobs": [job.to_dict() for job in jobs]}

    return app


async def run_server(config: ConfigLoader, *, host: str, port: int) -> None:
    import uvicorn

    app = build_app(config)
    uv_config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(uv_config)
    await server.serve()
