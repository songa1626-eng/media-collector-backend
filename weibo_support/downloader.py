from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from weibo_support.models import MediaItem

logger = logging.getLogger(__name__)


class AsyncDownloader:
    """通用异步批量下载引擎。"""

    def __init__(self, max_concurrent: int = 5, timeout: int = 60):
        self.max_concurrent = max_concurrent
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    )
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        name = name.replace("\n", " ").replace("\r", " ")
        name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name)
        name = re.sub(r'\s+', ' ', name).strip()
        if len(name) > 200:
            ext_idx = name.rfind(".")
            if ext_idx > 0:
                ext = name[ext_idx:]
                name = name[:200 - len(ext)] + ext
            else:
                name = name[:200]
        return name

    @staticmethod
    def _resolve_filename(item: MediaItem, index: int) -> str:
        if item.filename:
            return item.filename
        parsed = urlparse(item.url)
        path_name = Path(parsed.path).name
        if path_name and "." in path_name:
            return path_name
        ext = ".jpg" if item.media_type.value == "image" else ".mp4"
        return f"{item.source_id or index:04}{ext}"

    @classmethod
    def _resolve_save_dir(cls, root: Path, item: MediaItem) -> Path:
        relative_dir = str(item.extra.get("relative_dir") or "").strip()
        if not relative_dir:
            return root
        # Providers may request one child folder, but never get to provide a
        # path. Replacing separators also prevents traversal outside root.
        safe_dir = cls._sanitize_filename(relative_dir).strip(" .")[:180]
        return root / (safe_dir or "未命名内容")

    @staticmethod
    def _is_valid_media_content(item: MediaItem, content: bytes, content_type: str = "") -> bool:
        if not content:
            return False
        lower_type = content_type.lower().split(";", 1)[0].strip()
        stripped = content[:128].lstrip().lower()
        if stripped.startswith((b"<!doctype html", b"<html", b"<?xml")):
            return False

        if item.media_type.value == "image":
            has_image_signature = (
                content.startswith(b"\xff\xd8\xff")
                or content.startswith(b"\x89PNG\r\n\x1a\n")
                or content.startswith((b"GIF87a", b"GIF89a"))
                or (content.startswith(b"RIFF") and content[8:12] == b"WEBP")
                or content.startswith(b"BM")
            )
            return has_image_signature or lower_type.startswith("image/")

        has_video_signature = (
            (len(content) >= 12 and content[4:8] == b"ftyp")
            or content.startswith(b"\x1aE\xdf\xa3")
            or content.startswith(b"FLV")
            or content.startswith(b"\x00\x00\x01\xba")
        )
        return has_video_signature or lower_type.startswith("video/")

    @staticmethod
    def _is_hls_playlist(content: bytes) -> bool:
        """Return whether a text response is an HLS playlist rather than an error page."""
        return content.lstrip().startswith(b"#EXTM3U")

    async def _download_hls_video(
        self,
        item: MediaItem,
        filepath: Path,
        headers: dict[str, str] | None,
    ) -> tuple[bool, str]:
        """Remux a public HLS stream to MP4 without exposing credentials to a shell.

        Weibo can expose embedded-provider videos as an M3U8 playlist with a
        ``text/plain`` Content-Type.  ``imageio-ffmpeg`` bundles a local FFmpeg
        executable, avoiding a separate system-level installation.
        """
        try:
            import imageio_ffmpeg
        except ImportError:
            return False, "HLS 视频组件未安装，无法保存该视频"

        try:
            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return False, "HLS 视频组件不可用，无法保存该视频"

        filepath.parent.mkdir(parents=True, exist_ok=True)
        part_path = filepath.with_name(f"{filepath.stem}.part{filepath.suffix or '.mp4'}")
        playlist_dir: tempfile.TemporaryDirectory[str] | None = None
        try:
            if part_path.exists():
                part_path.unlink()
            session = await self._get_session()
            async with session.get(item.url, headers=headers) as response:
                if response.status != 200:
                    return False, f"HLS 播放列表请求失败（HTTP {response.status}）"
                playlist = await response.read()
                if not self._is_hls_playlist(playlist):
                    return False, "HLS 播放列表无效"
                playlist_url = str(response.url)

            # The source endpoint has no .m3u8 suffix and advertises
            # text/plain, so FFmpeg cannot auto-detect it.  Persist a local
            # playlist with absolute segment URLs to make the protocol and
            # container explicit without changing the source stream itself.
            playlist_dir = tempfile.TemporaryDirectory(prefix="chd-hls-")
            manifest_path = Path(playlist_dir.name) / "playlist.m3u8"
            manifest_lines = []
            for raw_line in playlist.decode("utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    line = re.sub(
                        r'(URI=")([^"]+)(")',
                        lambda match: f'{match.group(1)}{urljoin(playlist_url, match.group(2))}{match.group(3)}',
                        line,
                    )
                else:
                    line = urljoin(playlist_url, line)
                manifest_lines.append(line)
            manifest_path.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
            command = [
                ffmpeg_path,
                "-nostdin",
                "-y",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file,http,https,tcp,tls,crypto,data",
            ]
            command.extend([
                "-i",
                str(manifest_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(part_path),
            ])
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=self.timeout.total)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return False, "HLS 视频下载超时"
            if process.returncode != 0:
                return False, "HLS 视频转封装失败"
            if not part_path.exists():
                return False, "HLS 视频未生成媒体文件"
            with part_path.open("rb") as media_file:
                media_head = media_file.read(512)
            if not self._is_valid_media_content(item, media_head, "video/mp4"):
                return False, "HLS 视频转封装后不是有效视频"
            part_path.replace(filepath)
            return True, str(filepath)
        except Exception as exc:
            logger.warning("HLS video download failed: %s", exc)
            return False, "HLS 视频下载失败"
        finally:
            if playlist_dir is not None:
                playlist_dir.cleanup()
            if part_path.exists() and part_path != filepath:
                try:
                    part_path.unlink()
                except OSError:
                    pass

    async def download_one(
        self,
        item: MediaItem,
        save_dir: Path,
        index: int = 0,
        headers: dict[str, str] | None = None,
        max_retries: int = 3,
        inspect_callback: Any = None,
    ) -> tuple[bool, str]:
        """下载单个文件，返回 (是否成功, 文件路径或错误信息)。"""
        save_dir = self._resolve_save_dir(save_dir, item)
        filename = self._sanitize_filename(self._resolve_filename(item, index))
        filepath = save_dir / filename

        if filepath.exists():
            try:
                with filepath.open("rb") as existing_file:
                    existing_head = existing_file.read(512)
                if self._is_valid_media_content(item, existing_head):
                    return True, str(filepath)
            except OSError:
                pass

        session = await self._get_session()
        last_error = ""
        for attempt in range(1, max_retries + 1):
            try:
                async with session.get(item.url, headers=headers) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        content_type = resp.headers.get("Content-Type", "")
                        if (
                            item.media_type.value == "video"
                            and self._is_hls_playlist(content)
                        ):
                            ok, message = await self._download_hls_video(item, filepath, headers)
                            if ok:
                                if inspect_callback:
                                    result = await inspect_callback(item, filepath, "video/mp4")
                                    decision = result.get("decision") if isinstance(result, dict) else getattr(result, "decision", "")
                                    reason = result.get("reason", "") if isinstance(result, dict) else getattr(result, "reason", "")
                                    if decision == "filtered":
                                        try:
                                            filepath.unlink()
                                        except OSError:
                                            pass
                                        return False, f"__FILTERED__:{reason or '未检测到人物'}"
                                    if decision == "error":
                                        try:
                                            filepath.unlink()
                                        except OSError:
                                            pass
                                        return False, reason or "人像检测失败"
                                return True, message
                            last_error = message
                            continue
                        if not self._is_valid_media_content(item, content, content_type):
                            media_name = "图片" if item.media_type.value == "image" else "视频"
                            last_error = (
                                f"响应不是有效{media_name}"
                                f"（Content-Type: {content_type or 'unknown'}）"
                            )
                            continue
                        if inspect_callback:
                            suffix = Path(filename).suffix or (".jpg" if item.media_type.value == "image" else ".mp4")
                            temp_path = None
                            try:
                                with tempfile.NamedTemporaryFile(prefix="chd-detect-", suffix=suffix, delete=False) as temp_file:
                                    temp_file.write(content)
                                    temp_path = Path(temp_file.name)
                                result = await inspect_callback(item, temp_path, content_type)
                                decision = result.get("decision") if isinstance(result, dict) else getattr(result, "decision", "")
                                reason = result.get("reason", "") if isinstance(result, dict) else getattr(result, "reason", "")
                                if decision == "filtered":
                                    return False, f"__FILTERED__:{reason or '未检测到人物'}"
                                if decision == "error":
                                    return False, reason or "人像检测失败"
                            finally:
                                if temp_path:
                                    try:
                                        temp_path.unlink()
                                    except OSError:
                                        pass
                        save_dir.mkdir(parents=True, exist_ok=True)
                        filepath.write_bytes(content)
                        return True, str(filepath)
                    last_error = f"HTTP {resp.status}"
                    if resp.status in (403, 404):
                        break
            except Exception as e:
                last_error = str(e)
                logger.warning("Download attempt %d/%d failed %s: %s", attempt, max_retries, item.url[:100], e)

            if attempt < max_retries:
                await asyncio.sleep(2 * attempt)

        return False, last_error

    async def download_batch(
        self,
        items: list[MediaItem],
        save_dir: Path,
        headers: dict[str, str] | None = None,
        progress_callback: Any = None,
        inspect_callback: Any = None,
        filtered_callback: Any = None,
    ) -> tuple[int, int, list[dict]]:
        """批量下载，返回 (成功数, 失败数, 失败详情列表)。"""
        semaphore = asyncio.Semaphore(self.max_concurrent)
        success_count = 0
        fail_count = 0
        filtered_count = 0
        failed_details: list[dict] = []

        async def _download_with_sem(item: MediaItem, idx: int) -> tuple[MediaItem, bool, str]:
            async with semaphore:
                ok, msg = await self.download_one(item, save_dir, idx, headers, inspect_callback=inspect_callback)
                if ok:
                    logger.debug("Downloaded: %s", msg)
                elif msg.startswith("__FILTERED__:"):
                    logger.info("Filtered without person: %s", item.filename or item.url[:80])
                else:
                    logger.warning("Failed: %s -> %s", item.filename or item.url[:80], msg)
                return item, ok, msg

        tasks_list = [_download_with_sem(item, i) for i, item in enumerate(items)]

        for coro in asyncio.as_completed(tasks_list):
            item, ok, msg = await coro
            if ok:
                success_count += 1
            elif msg.startswith("__FILTERED__:"):
                filtered_count += 1
                if filtered_callback:
                    await filtered_callback(item, msg.split(":", 1)[1] or "未检测到人物")
            else:
                fail_count += 1
                failed_details.append({
                    "filename": item.filename or "unknown",
                    "source_id": item.source_id or "",
                    "url": item.url,
                    "error": msg,
                })
            if progress_callback:
                await progress_callback(success_count + fail_count + filtered_count, len(items))

        return success_count, fail_count, failed_details
