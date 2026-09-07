import tempfile
import unittest
from pathlib import Path

from weibo_support.downloader import AsyncDownloader
from weibo_support.models import MediaItem, MediaType


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str):
        self.status = 200
        self.headers = {"Content-Type": content_type}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def read(self):
        return self._body


class _FakeSession:
    closed = False

    def __init__(self, response: _FakeResponse):
        self.response = response

    def get(self, *_args, **_kwargs):
        return self.response


class DownloaderValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_html_body_returned_for_video(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = AsyncDownloader()
            downloader._session = _FakeSession(
                _FakeResponse(b"<!DOCTYPE html><html>denied</html>", "text/html")
            )
            item = MediaItem(
                url="https://example.invalid/video.mp4",
                media_type=MediaType.VIDEO,
                filename="video.mp4",
                source_id="video",
            )

            ok, message = await downloader.download_one(
                item, Path(tmpdir), max_retries=1
            )

            self.assertFalse(ok)
            self.assertIn("有效视频", message)
            self.assertFalse((Path(tmpdir) / "video.mp4").exists())

    async def test_routes_hls_playlist_video_to_hls_downloader(self):
        """Some Weibo videos are an HLS playlist served as text/plain."""
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = AsyncDownloader()
            downloader._session = _FakeSession(
                _FakeResponse(
                    b"#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXTINF:3,\nsegment.ts\n#EXT-X-ENDLIST\n",
                    "text/plain;charset=utf-8",
                )
            )
            item = MediaItem(
                url="https://example.invalid/video-playlist",
                media_type=MediaType.VIDEO,
                filename="video.mp4",
                source_id="hls-video",
            )
            calls = []

            async def download_hls(received_item, filepath, headers):
                calls.append((received_item, filepath, headers))
                return True, str(filepath)

            downloader._download_hls_video = download_hls
            ok, path = await downloader.download_one(
                item, Path(tmpdir), headers={"Referer": "https://weibo.com/"}, max_retries=1
            )

            self.assertTrue(ok)
            self.assertEqual(path, str(Path(tmpdir) / "video.mp4"))
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], item)
            self.assertEqual(calls[0][1], Path(tmpdir) / "video.mp4")

    async def test_hls_video_still_uses_person_filter_before_kept(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = AsyncDownloader()
            downloader._session = _FakeSession(
                _FakeResponse(b"#EXTM3U\n#EXT-X-ENDLIST\n", "text/plain")
            )
            item = MediaItem(
                url="https://example.invalid/video-playlist",
                media_type=MediaType.VIDEO,
                filename="video.mp4",
                source_id="hls-filtered",
            )

            async def download_hls(_item, filepath, _headers):
                filepath.write_bytes(b"\x00\x00\x00\x20ftypisomvideo")
                return True, str(filepath)

            async def inspect(_item, _temp_path, _content_type):
                return {"decision": "filtered", "reason": "未检测到人物"}

            downloader._download_hls_video = download_hls
            ok, message = await downloader.download_one(
                item, Path(tmpdir), inspect_callback=inspect, max_retries=1
            )

            self.assertFalse(ok)
            self.assertTrue(message.startswith("__FILTERED__:"))
            self.assertFalse((Path(tmpdir) / "video.mp4").exists())

    async def test_downloads_item_into_its_relative_post_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = AsyncDownloader()
            downloader._session = _FakeSession(
                _FakeResponse(b"\xff\xd8\xff\xe0image-data", "image/jpeg")
            )
            item = MediaItem(
                url="https://example.invalid/image.jpg",
                media_type=MediaType.IMAGE,
                filename="01.jpg",
                source_id="post_picture",
                extra={"relative_dir": "2026-08-22_一条微博_post"},
            )

            ok, path = await downloader.download_one(item, Path(tmpdir))

            self.assertTrue(ok)
            self.assertEqual(
                Path(path),
                Path(tmpdir) / "2026-08-22_一条微博_post" / "01.jpg",
            )
            self.assertTrue(Path(path).exists())

    async def test_person_filter_can_skip_without_counting_as_download_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader = AsyncDownloader()
            downloader._session = _FakeSession(
                _FakeResponse(b"\xff\xd8\xff\xe0image-data", "image/jpeg")
            )
            item = MediaItem(
                url="https://example.invalid/image.jpg",
                media_type=MediaType.IMAGE,
                filename="filtered.jpg",
                source_id="filtered",
            )
            filtered = []

            async def inspect(_item, temp_path, _content_type):
                self.assertTrue(temp_path.exists())
                return {"decision": "filtered", "reason": "未检测到人物"}

            async def mark_filtered(filtered_item, reason):
                filtered.append((filtered_item.source_id, reason))

            progress = []

            async def report_progress(done, total):
                progress.append((done, total))

            success, failed, details = await downloader.download_batch(
                [item], Path(tmpdir), inspect_callback=inspect, filtered_callback=mark_filtered,
                progress_callback=report_progress,
            )

            self.assertEqual((success, failed, details), (0, 0, []))
            self.assertEqual(filtered, [("filtered", "未检测到人物")])
            self.assertEqual(progress, [(1, 1)])
            self.assertFalse((Path(tmpdir) / "filtered.jpg").exists())


if __name__ == "__main__":
    unittest.main()

