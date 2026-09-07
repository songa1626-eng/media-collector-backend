from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from weibo_support.base_provider import BaseProvider
from weibo_support.models import MediaItem, MediaType, TargetInfo
from weibo_support.secure_cookie_store import ProtectedCookieStore

logger = logging.getLogger(__name__)

_BASE_URL = "https://m.weibo.cn/api/container/getIndex"
_BROWSER_DATA_DIR = str(Path(__file__).parent.parent / "data" / "weibo_browser")
_executor = ThreadPoolExecutor(max_workers=1)
_LOGIN_PAGE_MARKERS = ("扫描二维码登录", "账号登录", "登录/注册")
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
_PROTECTED_COOKIE_PATH = Path(__file__).parent.parent / "data" / "weibo_cookie.protected"


def _launch_weibo_browser(browser_type: Any, *, visible: bool) -> Any:
    """Launch the persisted login profile in Edge, with Chromium fallback."""
    options = {
        "headless": not visible,
        "args": ["--disable-blink-features=AutomationControlled"],
        "locale": "zh-CN",
        "user_agent": _BROWSER_USER_AGENT,
    }
    try:
        return browser_type.launch_persistent_context(
            _BROWSER_DATA_DIR, channel="msedge", **options
        )
    except Exception as exc:
        logger.warning("Microsoft Edge 启动失败，改用内置 Chromium：%s", exc)
        return browser_type.launch_persistent_context(_BROWSER_DATA_DIR, **options)


def _is_weibo_login_page(text: str) -> bool:
    """Return whether an HTML response is Weibo's interactive login page."""
    return any(marker in text for marker in _LOGIN_PAGE_MARKERS)


def _decode_weibo_api_response(text: str) -> dict | None:
    """Decode an API response, or return None when Weibo serves a login page."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if _is_weibo_login_page(text):
            return None
        raise ValueError(f"非 JSON 响应: {text[:200]}") from exc


def _build_weibo_feed_params(
    uid: str,
    container_id: str,
    *,
    page: int,
    since_id: str | None,
) -> dict[str, Any]:
    """Build a feed request compatible with old and current mobile APIs.

    Older responses provide a ``since_id`` cursor.  Current browser traffic
    uses numbered ``page`` requests instead, so use that as the fallback after
    the initial page.
    """
    params: dict[str, Any] = {
        "type": "uid",
        "value": uid,
        "containerid": container_id,
    }
    if since_id:
        params["since_id"] = since_id
    elif page > 1:
        params["page"] = page
    return params


def _iter_weibo_picture_dicts(pictures: Any) -> list[dict[str, Any]]:
    """Normalize Weibo's variable ``pics`` field into picture dictionaries."""
    if isinstance(pictures, list):
        candidates = pictures
    elif isinstance(pictures, dict):
        # A single picture has fields such as ``large`` and ``pid``.  Other
        # dictionaries are mappings whose values may be picture objects.
        if any(key in pictures for key in ("large", "url", "pid")):
            candidates = [pictures]
        else:
            candidates = pictures.values()
    else:
        candidates = []

    return [picture for picture in candidates if isinstance(picture, dict)]


def _iter_weibo_post_picture_dicts(mblog: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every picture declared by a post, retaining the post's order.

    Timeline cards commonly contain only the first nine objects in ``pics``.
    Full post payloads expose all media through ``pic_ids`` and ``pic_infos``.
    """
    by_id: dict[str, dict[str, Any]] = {}
    discovered_ids: list[str] = []
    anonymous: list[dict[str, Any]] = []

    def add_picture(value: Any, fallback_id: str = "") -> None:
        if not isinstance(value, dict):
            return
        picture = dict(value)
        picture_id = str(picture.get("pid") or picture.get("pic_id") or fallback_id).strip()
        if picture_id:
            picture.setdefault("pid", picture_id)
            existing = by_id.get(picture_id, {})
            merged = dict(existing)
            merged.update(picture)
            by_id[picture_id] = merged
            if picture_id not in discovered_ids:
                discovered_ids.append(picture_id)
            return
        anonymous.append(picture)

    for picture in _iter_weibo_picture_dicts(mblog.get("pics")):
        add_picture(picture)

    picture_infos = mblog.get("pic_infos")
    if isinstance(picture_infos, dict):
        if any(key in picture_infos for key in ("large", "largest", "original", "url", "pid")):
            add_picture(picture_infos)
        else:
            for picture_id, picture in picture_infos.items():
                add_picture(picture, str(picture_id))
    elif isinstance(picture_infos, list):
        for picture in picture_infos:
            add_picture(picture)

    ordered_ids = [str(value).strip() for value in mblog.get("pic_ids", []) if str(value).strip()]
    result: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for picture_id in ordered_ids + discovered_ids:
        if picture_id in used_ids or picture_id not in by_id:
            continue
        used_ids.add(picture_id)
        result.append(by_id[picture_id])
    return result + anonymous


def _weibo_picture_url(picture: dict[str, Any]) -> str:
    """Prefer the largest available image URL from legacy and detail payloads."""
    for key in ("largest", "original", "large", "bmiddle", "thumbnail"):
        value = picture.get(key)
        if isinstance(value, dict) and isinstance(value.get("url"), str) and value["url"]:
            return value["url"]
    value = picture.get("url")
    return value if isinstance(value, str) else ""


def _weibo_declared_picture_count(mblog: dict[str, Any]) -> int:
    try:
        return max(0, int(mblog.get("pic_num") or 0))
    except (TypeError, ValueError):
        return 0


def _parse_weibo_detail_status(page_text: str) -> dict[str, Any] | None:
    """Extract the full status object embedded in a mobile detail page."""
    match = re.search(r"var\s+\$render_data\s*=\s*(\[.*?\])\s*\[0\]", page_text, re.DOTALL)
    if not match:
        return None
    try:
        records = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(records, list):
        return None
    for record in records:
        if not isinstance(record, dict):
            continue
        status = record.get("status")
        if isinstance(status, dict):
            return status
        data = record.get("data")
        if isinstance(data, dict) and isinstance(data.get("status"), dict):
            return data["status"]
    return None


def _iter_weibo_mblogs(value: Any) -> list[dict[str, Any]]:
    """Collect post objects from both direct cards and nested card groups."""
    posts: list[dict[str, Any]] = []

    def collect(current: Any) -> None:
        if isinstance(current, list):
            for item in current:
                collect(item)
            return
        if not isinstance(current, dict):
            return
        mblog = current.get("mblog")
        if isinstance(mblog, dict):
            posts.append(mblog)
        for key in ("card_group", "cards"):
            collect(current.get(key))

    collect(value)
    return posts


def _is_pinned_weibo_post(mblog: dict[str, Any]) -> bool:
    """Return whether a card is a profile's non-chronological pinned post.

    The mobile API exposes this flag with different spellings across response
    variants. Pinned posts can appear before the latest timeline work, so they
    must never become an update-check boundary.
    """
    for key in ("isTop", "is_top", "is_pinned", "pinned"):
        value = mblog.get(key)
        if isinstance(value, str):
            if value.strip().lower() in {"1", "true", "yes", "on"}:
                return True
        elif bool(value):
            return True
    return "置顶" in str(mblog.get("title_source") or mblog.get("title") or "")


def _plain_weibo_text(value: Any) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def _safe_post_folder(mblog: dict[str, Any]) -> str:
    """Mirror Douyin's date-title-id folder layout with Windows-safe text."""
    post_id = str(mblog.get("id") or mblog.get("mid") or "post")
    title = _plain_weibo_text(mblog.get("text")) or "无文字"
    title = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", title)
    title = re.sub(r"\s+", " ", title).strip(" ._")[:60] or "无文字"

    date_text = ""
    created_at = str(mblog.get("created_at") or "").strip()
    if created_at:
        try:
            date_text = parsedate_to_datetime(created_at).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OverflowError):
            match = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", created_at)
            if match:
                date_text = f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"

    parts = [part for part in (date_text, title, post_id) if part]
    return "_".join(parts)[:150].rstrip(" .") or f"微博_{post_id}"


def _weibo_post_date(mblog: dict[str, Any]) -> date | None:
    """Return the post day in the same format used by its local folder."""
    created_at = str(mblog.get("created_at") or "").strip()
    if not created_at:
        return None
    try:
        return parsedate_to_datetime(created_at).date()
    except (TypeError, ValueError, OverflowError):
        match = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", created_at)
        if not match:
            return None
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None


def _weibo_video_url(mblog: dict[str, Any]) -> str:
    page_info = mblog.get("page_info")
    media_info = page_info.get("media_info") if isinstance(page_info, dict) else None
    if not isinstance(media_info, dict):
        return ""

    for key in ("stream_url_hd", "stream_url", "mp4_hd_url", "mp4_sd_url"):
        value = media_info.get(key)
        if isinstance(value, str) and value:
            return value

    urls = media_info.get("urls")
    if isinstance(urls, dict):
        for value in urls.values():
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict) and isinstance(value.get("url"), str):
                return value["url"]
    return ""


class _SyncWeiboBrowserSession:
    """A single executor-owned Playwright context reused across Weibo pages."""

    def __init__(self) -> None:
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._visible = False

    def _open(self, *, visible: bool) -> None:
        from playwright.sync_api import sync_playwright

        Path(_BROWSER_DATA_DIR).mkdir(parents=True, exist_ok=True)
        if self._context:
            self._context.close()
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        self._context = _launch_weibo_browser(self._playwright.chromium, visible=visible)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._visible = visible

    def _add_cookie(self, cookie: str) -> None:
        if not cookie or not self._context:
            return
        cookies = []
        for part in cookie.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            for domain in (".weibo.cn", ".weibo.com"):
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": domain,
                    "path": "/",
                })
        if cookies:
            self._context.add_cookies(cookies)

    def request(self, url: str, cookie: str) -> dict:
        if self._context is None:
            self._open(visible=False)
        self._add_cookie(cookie)

        # API pages can be answered with a non-2xx challenge/rate-limit page.
        # ``page.goto`` raises ``ERR_HTTP_RESPONSE_CODE_FAILURE`` before the
        # response body is available, so use the context request client first.
        # It shares the persistent browser cookies but lets us inspect the
        # body and decide whether the visible login flow is actually needed.
        data = None
        try:
            response = self._context.request.get(
                url,
                headers={
                    "Referer": "https://m.weibo.cn/",
                    "Accept": "application/json,text/plain,*/*",
                },
                timeout=30000,
            )
            data = _decode_weibo_api_response(response.text())
        except Exception as exc:
            logger.warning("微博移动接口请求异常，准备使用浏览器流程重试：%s", exc)

        if data is not None and data.get("ok") != -100 and data.get("errno") != "-100":
            return data, self.cookie_header()

        redirect_url = data.get("url", "") if data else ""
        if not self._visible:
            self._open(visible=True)
            self._add_cookie(cookie)

        logger.warning("需要验证码/登录，弹出浏览器窗口...")
        if "captcha" in redirect_url:
            self._page.goto(redirect_url)
        else:
            self._page.goto("https://m.weibo.cn/")

        logger.info("请在弹出的浏览器中完成验证码/登录（最多等待 120 秒）...")
        for i in range(120):
            self._page.wait_for_timeout(1000)
            current_url = self._page.url
            page_text = self._page.inner_text("body")
            if (
                "captcha" not in current_url
                and "login" not in current_url
                and not _is_weibo_login_page(page_text)
                and i > 3
            ):
                self._page.goto(url, wait_until="domcontentloaded")
                data = _decode_weibo_api_response(self._page.inner_text("body"))
                if data is not None and data.get("ok") == 1:
                    return data, self.cookie_header()
            if i % 10 == 0:
                logger.info("  等待中... (%ds) url=%s", i, current_url[:80])

        raise ValueError("登录超时或未完成，请在弹出的微博窗口中登录后重试")

    def api_request(self, url: str, cookie: str) -> tuple[dict, str]:
        """Issue an authenticated Ajax request with the desktop page as referer."""
        if self._context is None:
            self._open(visible=False)
        self._add_cookie(cookie)
        response = self._context.request.get(
            url,
            headers={"Referer": "https://weibo.com/"},
            timeout=30000,
        )
        data = _decode_weibo_api_response(response.text())
        if data is None:
            # Reuse the visible-login path, then repeat through the shared API
            # request context once the browser session has been refreshed.
            self.request(url, cookie)
            response = self._context.request.get(
                url,
                headers={"Referer": "https://weibo.com/"},
                timeout=30000,
            )
            data = _decode_weibo_api_response(response.text())
        if data is None:
            raise ValueError("微博未返回可解析的接口数据")
        return data, self.cookie_header()

    def detail_page_html(self, url: str, cookie: str) -> tuple[str, str]:
        """Read a mobile detail page through the persisted logged-in context."""
        if self._context is None:
            self._open(visible=False)
        self._add_cookie(cookie)
        response = self._context.request.get(
            url,
            headers={"Referer": "https://m.weibo.cn/"},
            timeout=30000,
        )
        return response.text(), self.cookie_header()

    def cookie_header(self) -> str:
        """Export Weibo cookies only for protected local reuse, never for UI."""
        if not self._context:
            return ""
        cookies = self._context.cookies()
        return "; ".join(
            f"{item['name']}={item['value']}"
            for item in cookies
            if "weibo" in item.get("domain", "").lower()
        )

    def current_account_uid(self, cookie: str) -> tuple[str, str]:
        """Resolve the logged-in desktop account without exposing it to the UI."""
        if self._context is None:
            self._open(visible=False)
        self._add_cookie(cookie)
        self._page.goto("https://weibo.com/", wait_until="domcontentloaded")
        self._page.wait_for_timeout(2500)
        uid = self._page.evaluate(
            "() => String(window.$CONFIG?.uid || window.$CONFIG?.user?.id || "
            "window.__INITIAL_STATE__?.user?.id || '')"
        )
        if not str(uid or "").isdigit():
            raise ValueError("无法读取当前微博账号，请重新登录后重试")
        return str(uid), self.cookie_header()

    def close(self) -> None:
        if self._context:
            self._context.close()
        self._context = None
        self._page = None
        self._visible = False
        if self._playwright:
            self._playwright.stop()
        self._playwright = None


_browser_session = _SyncWeiboBrowserSession()


def _sync_browser_request(url: str, cookie: str, need_login: bool = False) -> tuple[dict, str]:
    """Request through the persistent executor-owned browser session."""
    return _browser_session.request(url, cookie)


def _sync_close_browser_session() -> None:
    _browser_session.close()


def _sync_browser_current_uid(cookie: str) -> tuple[str, str]:
    return _browser_session.current_account_uid(cookie)


def _sync_browser_api_request(url: str, cookie: str) -> tuple[dict, str]:
    return _browser_session.api_request(url, cookie)


def _sync_browser_detail_request(url: str, cookie: str) -> tuple[str, str]:
    return _browser_session.detail_page_html(url, cookie)


def _legacy_sync_browser_request(url: str, cookie: str, need_login: bool = False) -> dict:
    """Legacy isolated request implementation retained for source compatibility."""
    from playwright.sync_api import sync_playwright

    Path(_BROWSER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = _launch_weibo_browser(p.chromium, visible=need_login)

        if cookie:
            cookies = []
            for part in cookie.split(";"):
                part = part.strip()
                if "=" not in part:
                    continue
                name, value = part.split("=", 1)
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".weibo.cn",
                    "path": "/",
                })
            if cookies:
                context.add_cookies(cookies)

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        text = page.inner_text("body")

        data = _decode_weibo_api_response(text)

        if data is None or data.get("ok") == -100 or data.get("errno") == "-100":
            context.close()

            logger.warning("需要验证码/登录，弹出浏览器窗口...")
            context2 = _launch_weibo_browser(p.chromium, visible=True)

            page2 = context2.pages[0] if context2.pages else context2.new_page()
            redirect_url = data.get("url", "") if data else ""
            if "captcha" in redirect_url:
                page2.goto(redirect_url)
            else:
                page2.goto("https://m.weibo.cn/")

            logger.info("请在弹出的浏览器中完成验证码/登录（最多等待 120 秒）...")
            for i in range(120):
                page2.wait_for_timeout(1000)
                current_url = page2.url
                page_text = page2.inner_text("body")
                still_on_login_page = _is_weibo_login_page(page_text)
                if "captcha" not in current_url and "login" not in current_url and not still_on_login_page and i > 3:
                    page2.goto(url, wait_until="domcontentloaded")
                    data = _decode_weibo_api_response(page2.inner_text("body"))
                    if data is not None and data.get("ok") == 1:
                        context2.close()
                        return data
                if i % 10 == 0:
                    logger.info("  等待中... (%ds) url=%s", i, current_url[:80])

            context2.close()
            raise ValueError("登录超时或未完成，请在弹出的微博窗口中登录后重试")
        else:
            context.close()

        return data


class WeiboProvider(BaseProvider):
    name = "weibo"
    display_name = "微博"
    description = "从微博用户主页批量下载原图和视频（使用浏览器引擎）"

    def __init__(self) -> None:
        self._cookie_store = ProtectedCookieStore(_PROTECTED_COOKIE_PATH)
        self._cookie: str = self._cookie_store.load()

    def set_cookie(self, cookie: str) -> None:
        self._cookie = cookie

    async def _api_get(self, params: dict[str, Any]) -> dict:
        qs = urlencode(params)
        url = f"{_BASE_URL}?{qs}"
        loop = asyncio.get_event_loop()
        # The one-worker executor already serializes access to the persistent
        # Playwright session.  Acquiring threading.Lock here would block the
        # asyncio event loop while another request is waiting for the browser,
        # making even /api/health and task-status requests time out.
        data, refreshed_cookie = await loop.run_in_executor(
            _executor,
            _sync_browser_request,
            url,
            self._cookie,
            False,
        )
        if refreshed_cookie and refreshed_cookie != self._cookie:
            self._cookie = refreshed_cookie
            self._cookie_store.save(refreshed_cookie)
        return data

    async def _api_url_get(self, url: str) -> dict:
        loop = asyncio.get_event_loop()
        data, refreshed_cookie = await loop.run_in_executor(
            _executor, _sync_browser_api_request, url, self._cookie
        )
        if refreshed_cookie and refreshed_cookie != self._cookie:
            self._cookie = refreshed_cookie
            self._cookie_store.save(refreshed_cookie)
        return data

    async def _fetch_weibo_detail_mblog(self, post_id: str) -> dict[str, Any] | None:
        """Load the full post only when a timeline card truncates its pictures."""
        if not post_id:
            return None
        loop = asyncio.get_event_loop()
        page_text, refreshed_cookie = await loop.run_in_executor(
            _executor,
            _sync_browser_detail_request,
            f"https://m.weibo.cn/detail/{post_id}",
            self._cookie,
        )
        if refreshed_cookie and refreshed_cookie != self._cookie:
            self._cookie = refreshed_cookie
            self._cookie_store.save(refreshed_cookie)
        return _parse_weibo_detail_status(page_text)

    async def _expand_overflow_picture_posts(self, cards: list[Any]) -> None:
        """Merge full detail media into timeline cards that advertise over-nine images."""
        candidates: list[dict[str, Any]] = []
        for mblog in _iter_weibo_mblogs(cards):
            candidates.append(mblog)
            retweeted = mblog.get("retweeted_status")
            if isinstance(retweeted, dict):
                candidates.append(retweeted)

        for mblog in candidates:
            declared_count = _weibo_declared_picture_count(mblog)
            if declared_count <= len(_iter_weibo_post_picture_dicts(mblog)):
                continue
            post_id = str(mblog.get("id") or mblog.get("mid") or "").strip()
            if not post_id:
                continue
            try:
                detail = await self._fetch_weibo_detail_mblog(post_id)
            except Exception as exc:
                logger.warning("微博 %s 的超九图详情读取失败，保留时间线图片：%s", post_id, exc)
                continue
            if not isinstance(detail, dict):
                logger.warning("微博 %s 的超九图详情未包含可解析内容，保留时间线图片", post_id)
                continue
            for key in ("pics", "pic_ids", "pic_infos", "pic_num", "live_photo", "mix_media_info"):
                if key in detail:
                    mblog[key] = detail[key]

    async def _fetch_following_desktop(self) -> dict[str, Any]:
        """Use Weibo's desktop relationship API and retain its displayed count."""
        loop = asyncio.get_event_loop()
        uid, refreshed_cookie = await loop.run_in_executor(
            _executor, _sync_browser_current_uid, self._cookie
        )
        if refreshed_cookie and refreshed_cookie != self._cookie:
            self._cookie = refreshed_cookie
            self._cookie_store.save(refreshed_cookie)

        info = await self._api_url_get(f"https://weibo.com/ajax/profile/info?uid={uid}")
        user_info = ((info.get("data") or {}).get("user") or {}) if isinstance(info, dict) else {}
        reported_total = int(user_info.get("friends_count") or 0)
        profiles: list[dict[str, str]] = []
        seen: set[str] = set()
        readable_total = 0

        for page in range(1, 101):
            data = await self._api_url_get(
                f"https://weibo.com/ajax/friendships/friends?uid={uid}&page={page}"
            )
            users = data.get("users") if isinstance(data, dict) else []
            if not isinstance(users, list) or not users:
                break
            readable_total = max(readable_total, int(data.get("total_number") or 0))
            before = len(profiles)
            for user in users:
                if not isinstance(user, dict):
                    continue
                target_uid = str(user.get("idstr") or user.get("id") or "").strip()
                if not target_uid or target_uid in seen:
                    continue
                seen.add(target_uid)
                profiles.append({
                    "link": f"https://weibo.com/u/{target_uid}",
                    "name": str(user.get("screen_name") or target_uid).strip(),
                })
            if len(profiles) == before or (readable_total and len(profiles) >= readable_total):
                break
            await asyncio.sleep(random.uniform(0.35, 0.7))

        return {
            "profiles": profiles,
            "reported_total": reported_total or readable_total or len(profiles),
            "readable_total": readable_total or len(profiles),
        }

    @staticmethod
    def _extract_uid(url_or_id: str) -> str:
        url_or_id = url_or_id.strip()
        if url_or_id.isdigit():
            return url_or_id

        parsed = urlparse(url_or_id)
        host = (parsed.hostname or "").lower()
        if not host or not (
            host in {"weibo.com", "weibo.cn"}
            or host.endswith(".weibo.com")
            or host.endswith(".weibo.cn")
        ):
            raise ValueError(f"不是微博 URL: {url_or_id}")
        path = parsed.path.rstrip("/")

        m = re.search(r"/u/(\d+)", path)
        if m:
            return m.group(1)

        m = re.search(r"/profile/(\d+)", path)
        if m:
            return m.group(1)

        m = re.search(r"/(\d+)$", path)
        if m:
            return m.group(1)

        raise ValueError(f"无法从 URL 中提取用户 ID: {url_or_id}")

    async def parse_target(self, url_or_id: str) -> TargetInfo:
        uid = self._extract_uid(url_or_id)
        logger.info("parse_target: uid=%s", uid)

        display_name = uid
        avatar_url = ""
        container_id = f"107603{uid}"

        try:
            data = await self._api_get({"type": "uid", "value": uid})

            if data.get("ok") == 1:
                user_info = data.get("data", {}).get("userInfo", {})
                display_name = user_info.get("screen_name", uid)
                avatar_url = user_info.get("avatar_hd", "")
                for tab in data.get("data", {}).get("tabsInfo", {}).get("tabs", []):
                    if tab.get("tab_type") == "weibo":
                        container_id = tab.get("containerid", container_id)
                        break
        except Exception as e:
            logger.warning("parse_target failed for %s: %s (will use defaults)", uid, e)

        return TargetInfo(
            provider=self.name,
            target_id=uid,
            display_name=display_name,
            avatar_url=avatar_url,
            extra={"container_id": container_id},
        )

    async def fetch_following(self) -> dict[str, Any]:
        """Return the current logged-in account's public following list."""
        try:
            desktop = await self._fetch_following_desktop()
            if desktop.get("profiles"):
                return desktop
        except Exception as exc:
            logger.warning("desktop following API unavailable; use mobile fallback: %s", exc)

        profiles = []
        seen = set()
        since_id = None
        reported_total = 0

        def collect_users(value: Any) -> None:
            """Following responses may nest user cards under card_group."""
            if isinstance(value, list):
                for item in value:
                    collect_users(item)
                return
            if not isinstance(value, dict):
                return
            user = value.get("user") or value.get("userInfo")
            if isinstance(user, dict):
                uid = str(user.get("id") or user.get("idstr") or "").strip()
                if uid and uid not in seen:
                    seen.add(uid)
                    profiles.append({
                        "link": f"https://weibo.com/u/{uid}",
                        "name": str(user.get("screen_name") or uid).strip(),
                    })
            for group_key in ("card_group", "cards"):
                if group_key in value:
                    collect_users(value.get(group_key))

        for page in range(1, 101):
            params = {"containerid": "231093_-_selffollowed"}
            if since_id:
                params["since_id"] = since_id
            elif page > 1:
                params["page"] = page
            data = await self._api_get(params)
            if data.get("ok") != 1:
                if page == 1:
                    message = data.get("msg") or data.get("message") or "微博未返回关注列表"
                    raise ValueError(f"{message}，请在弹出的微博窗口完成登录后重试")
                break
            cards = data.get("data", {}).get("cards", [])
            if not cards:
                break
            profile_count_before = len(profiles)
            collect_users(cards)
            # Current mobile responses may omit pagination metadata.  Keep
            # requesting numbered pages until cards are empty; respect an
            # explicit has_more=false when it is available.
            card_info = data.get("data", {}).get("cardlistInfo", {})
            reported_total = max(reported_total, int(card_info.get("total") or 0))
            has_more = card_info.get("has_more")
            if has_more in (0, "0", False):
                break
            next_since_id = str(card_info.get("since_id") or "").strip() or None
            if next_since_id:
                if next_since_id == since_id:
                    logger.warning("following cursor did not advance; stop paging")
                    break
                since_id = next_since_id
            elif len(profiles) == profile_count_before:
                # Some responses omit all pagination metadata.  A numbered
                # request that adds no new users is the same page repeated;
                # continuing to page 100 only delays the UI until it times
                # out and cannot discover additional follows.
                logger.warning("following page repeated without a cursor; stop paging")
                break
            await asyncio.sleep(random.uniform(1.0, 2.0))
        return {
            "profiles": profiles,
            "reported_total": reported_total or len(profiles),
            "readable_total": len(profiles),
        }

    @staticmethod
    def _extract_media_items(
        cards: list[Any], *, start_date: date | None = None, end_date: date | None = None
    ) -> list[MediaItem]:
        media_items: list[MediaItem] = []
        seen_source_ids: set[str] = set()
        for mblog in _iter_weibo_mblogs(cards):
            post_date = _weibo_post_date(mblog)
            if start_date or end_date:
                if post_date is None:
                    continue
                if start_date and post_date < start_date:
                    continue
                if end_date and post_date > end_date:
                    continue
            outer_mid = str(mblog.get("id") or mblog.get("mid") or "")
            folder_name = _safe_post_folder(mblog)
            asset_index = 0

            sources = [(mblog, outer_mid)]
            retweeted = mblog.get("retweeted_status")
            if isinstance(retweeted, dict):
                retweeted_mid = str(retweeted.get("id") or retweeted.get("mid") or "original")
                sources.append((retweeted, f"{outer_mid}_retweet_{retweeted_mid}"))

            for source_mblog, source_prefix in sources:
                for pic in _iter_weibo_post_picture_dicts(source_mblog):
                    pid = str(pic.get("pid") or "")
                    live_url = pic.get("videoSrc")
                    is_live_photo = (
                        str(pic.get("type") or "").lower() == "livephoto"
                        or (bool(source_mblog.get("live_photo")) and bool(live_url))
                    )
                    if is_live_photo:
                        # The user explicitly wants the native motion asset,
                        # not the still cover paired with a Weibo Live Photo.
                        if isinstance(live_url, str) and live_url:
                            source_id = (
                                f"{source_prefix}_{pid}_live"
                                if pid else f"{source_prefix}_live_{asset_index + 1}"
                            )
                            if source_id not in seen_source_ids:
                                seen_source_ids.add(source_id)
                                asset_index += 1
                                media_items.append(
                                    MediaItem(
                                        url=live_url,
                                        media_type=MediaType.VIDEO,
                                        filename=f"{asset_index:02d}.mov",
                                        source_id=source_id,
                                        extra={
                                            "relative_dir": folder_name,
                                            "post_id": outer_mid,
                                            "live_photo": True,
                                        },
                                    )
                                )
                        continue

                    url = _weibo_picture_url(pic)
                    if not isinstance(url, str) or not url:
                        continue

                    source_id = f"{source_prefix}_{pid}" if pid else source_prefix or url
                    if source_id in seen_source_ids:
                        continue
                    seen_source_ids.add(source_id)
                    asset_index += 1
                    suffix = Path(urlparse(url).path).suffix.lower()
                    if suffix not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                        suffix = ".jpg"
                    media_items.append(
                        MediaItem(
                            url=url,
                            media_type=MediaType.IMAGE,
                            filename=f"{asset_index:02d}{suffix}",
                            source_id=source_id,
                            extra={
                                "relative_dir": folder_name,
                                "post_id": outer_mid,
                            },
                        )
                    )

                video_url = _weibo_video_url(source_mblog)
                if video_url:
                    source_id = f"{source_prefix}_video" if source_prefix else video_url
                    if source_id in seen_source_ids:
                        continue
                    seen_source_ids.add(source_id)
                    asset_index += 1
                    media_items.append(
                        MediaItem(
                            url=video_url,
                            media_type=MediaType.VIDEO,
                            filename=f"{asset_index:02d}.mp4",
                            source_id=source_id,
                            extra={
                                "relative_dir": folder_name,
                                "post_id": outer_mid,
                            },
                        )
                    )
        return media_items

    async def get_latest_update_source_ids(self, target: TargetInfo) -> list[str]:
        """Return latest Weibo post IDs, including text-only posts, without downloading."""
        container_id = target.extra.get("container_id", f"107603{target.target_id}")
        data = await self._api_get(
            _build_weibo_feed_params(target.target_id, container_id, page=1, since_id=None)
        )
        if data.get("ok") != 1:
            message = data.get("msg") or data.get("message") or "微博接口未返回作品列表"
            raise ValueError(
                f"{message}（可能触发了反爬限制），请稍后重试或在弹出的微博窗口完成登录后重试"
            )
        seen: set[str] = set()
        source_ids: list[str] = []
        cards = data.get("data", {}).get("cards", [])
        for mblog in _iter_weibo_mblogs(cards):
            if _is_pinned_weibo_post(mblog):
                continue
            post_id = str(mblog.get("id") or mblog.get("mid") or "").strip()
            if post_id and post_id not in seen:
                seen.add(post_id)
                source_ids.append(post_id)
        return source_ids

    async def get_update_ids_after(
        self, target: TargetInfo, downloaded_post_ids: set[str], *, max_pages: int = 120
    ) -> tuple[list[str], bool]:
        """Read platform pages until the latest successfully downloaded post is reached."""
        container_id = target.extra.get("container_id", f"107603{target.target_id}")
        page = 1
        since_id: str | None = None
        new_ids: list[str] = []
        seen: set[str] = set()
        for _ in range(max_pages):
            data = await self._api_get(
                _build_weibo_feed_params(target.target_id, container_id, page=page, since_id=since_id)
            )
            if data.get("ok") != 1:
                message = data.get("msg") or data.get("message") or "微博接口未返回作品列表"
                raise ValueError(f"{message}（可能触发了反爬限制），请稍后重试或完成登录后重试")
            cards = data.get("data", {}).get("cards", [])
            for mblog in _iter_weibo_mblogs(cards):
                if _is_pinned_weibo_post(mblog):
                    continue
                post_id = str(mblog.get("id") or mblog.get("mid") or "").strip()
                if not post_id or post_id in seen:
                    continue
                seen.add(post_id)
                if post_id in downloaded_post_ids:
                    return new_ids, True
                new_ids.append(post_id)
            card_list_info = data.get("data", {}).get("cardlistInfo", {})
            if not cards:
                break
            since_id = card_list_info.get("since_id")
            has_numbered_page = bool(card_list_info.get("page"))
            if not since_id and not has_numbered_page:
                break
            page += 1
        return new_ids, False

    async def iter_media_pages(
        self,
        target: TargetInfo,
        *,
        start_page: int = 1,
        end_page: int = 0,
        start_date: date | None = None,
        end_date: date | None = None,
    ):
        """Yield one parsed media page at a time for checkpointed downloads."""
        container_id = target.extra.get("container_id", f"107603{target.target_id}")
        page = max(1, start_page)
        since_id: str | None = None

        while True:
            params = _build_weibo_feed_params(
                target.target_id,
                container_id,
                page=page,
                since_id=since_id,
            )
            data = await self._api_get(params)
            if data.get("ok") != 1:
                message = data.get("msg") or data.get("message") or "微博接口未返回作品列表"
                raise ValueError(
                    f"微博第 {page} 页读取异常：{message}。未确认已经到达末页，"
                    "请稍后重试或在弹出的微博窗口完成登录后重试"
                )

            payload = data.get("data", {})
            card_list_info = payload.get("cardlistInfo", {}) if isinstance(payload, dict) else {}
            if not isinstance(card_list_info, dict):
                card_list_info = {}
            cards = payload.get("cards", []) if isinstance(payload, dict) else []
            if not cards:
                # A successful empty response ends the timeline only when
                # Weibo explicitly says there is no next page.  Some rate
                # control responses are transiently empty but otherwise look
                # successful, so confirm an unmarked empty page once.
                if card_list_info.get("has_more") in (0, "0", False):
                    return
                confirmation = await self._api_get(params)
                if confirmation.get("ok") != 1:
                    message = confirmation.get("msg") or confirmation.get("message") or "微博接口未返回作品列表"
                    raise ValueError(
                        f"微博第 {page} 页读取异常：{message}。未确认已经到达末页，"
                        "请稍后重试或在弹出的微博窗口完成登录后重试"
                    )
                confirmed_payload = confirmation.get("data", {})
                confirmed_cards = confirmed_payload.get("cards", []) if isinstance(confirmed_payload, dict) else []
                if not confirmed_cards:
                    return
                data = confirmation
                payload = confirmed_payload
                card_list_info = payload.get("cardlistInfo", {})
                if not isinstance(card_list_info, dict):
                    card_list_info = {}
                cards = confirmed_cards

            await self._expand_overflow_picture_posts(cards)
            page_items = self._extract_media_items(cards, start_date=start_date, end_date=end_date)
            yield page, page_items

            # Weibo pages are newest-first. Once an entire dated page is older
            # than the requested start day, no later page can re-enter range.
            post_dates = [_weibo_post_date(mblog) for mblog in _iter_weibo_mblogs(cards)]
            if start_date and post_dates and all(item is not None and item < start_date for item in post_dates):
                return

            if end_page and page >= end_page:
                return

            if card_list_info.get("has_more") in (0, "0", False):
                return

            since_id = card_list_info.get("since_id")
            has_numbered_page = bool(card_list_info.get("page"))
            page += 1
            if not since_id and not has_numbered_page:
                return

            if page % 20 == 1:
                await asyncio.sleep(random.randint(30, 60))
            else:
                await asyncio.sleep(random.uniform(2.5, 5.0))

    async def fetch_media_list(
        self,
        target: TargetInfo,
        *,
        start_page: int = 1,
        end_page: int = 0,
        start_date: date | None = None,
        end_date: date | None = None,
        downloaded_ids: set[str] | None = None,
        progress_callback: Any = None,
    ) -> list[MediaItem]:
        media_items: list[MediaItem] = []
        async for page, page_items in self.iter_media_pages(
            target, start_page=start_page, end_page=end_page, start_date=start_date, end_date=end_date
        ):
            if downloaded_ids:
                page_items = [item for item in page_items if item.source_id not in downloaded_ids]
            media_items.extend(page_items)
            if progress_callback:
                await progress_callback(
                    len(media_items),
                    f"第 {page} 页，{len(page_items)} 张图片",
                )
            logger.info(
                "Page %d: %d images (total collected: %d)",
                page, len(page_items), len(media_items),
            )

        return media_items

    def get_download_headers(self) -> dict[str, str] | None:
        headers = {"Referer": "https://weibo.com/"}
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    def get_config_schema(self) -> dict[str, Any]:
        return {
            "cookie": {
                "type": "string",
                "title": "微博 Cookie（可选）",
                "description": (
                    "可选：如果不填，首次运行时会弹出浏览器让你登录。"
                    "如需手动填写：登录 m.weibo.cn → F12 → Network → 复制 Cookie"
                ),
            }
        }

    async def validate_config(self, config: dict[str, Any]) -> bool:
        return True

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor, _sync_close_browser_session)
