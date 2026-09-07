import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from weibo_support.models import TargetInfo
from weibo_support.provider import (
    WeiboProvider,
    _build_weibo_feed_params,
    _decode_weibo_api_response,
    _is_weibo_login_page,
    _iter_weibo_picture_dicts,
    _launch_weibo_browser,
    _parse_weibo_detail_status,
    _SyncWeiboBrowserSession,
)


class WeiboLoginPageTests(unittest.TestCase):
    def test_mobile_api_uses_context_request_before_page_navigation(self):
        class Response:
            def text(self):
                return '{"ok": 1, "data": {"cards": []}}'

        class Request:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        class Context:
            def __init__(self):
                self.request = Request()

            def cookies(self):
                return []

            def add_cookies(self, _cookies):
                raise AssertionError("空 Cookie 不应写入")

        class Page:
            def goto(self, *_args, **_kwargs):
                raise AssertionError("接口 JSON 已由 context.request 读取，不应导航页面")

        session = _SyncWeiboBrowserSession()
        session._context = Context()
        session._page = Page()

        data, _cookie = session.request("https://m.weibo.cn/api/container/getIndex?page=2", "")

        self.assertEqual(data["ok"], 1)
        self.assertEqual(len(session._context.request.calls), 1)
        self.assertEqual(session._context.request.calls[0][1]["headers"]["Referer"], "https://m.weibo.cn/")

    def test_browser_request_returns_current_cookie_header(self):
        class Context:
            def cookies(self):
                return [
                    {"name": "keep", "value": "one", "domain": ".weibo.cn"},
                    {"name": "skip", "value": "two", "domain": ".example.com"},
                ]

        session = _SyncWeiboBrowserSession()
        session._context = Context()
        self.assertEqual(session.cookie_header(), "keep=one")

    def test_prefers_edge_for_the_browser_login_profile(self):
        class Browser:
            def __init__(self):
                self.calls = []

            def launch_persistent_context(self, *_args, **kwargs):
                self.calls.append(kwargs)
                return "edge-context"

        browser = Browser()

        self.assertEqual(_launch_weibo_browser(browser, visible=True), "edge-context")
        self.assertEqual(browser.calls[0]["channel"], "msedge")
        self.assertFalse(browser.calls[0]["headless"])

    def test_falls_back_to_chromium_when_edge_cannot_start(self):
        class Browser:
            def __init__(self):
                self.calls = []

            def launch_persistent_context(self, *_args, **kwargs):
                self.calls.append(kwargs)
                if "channel" in kwargs:
                    raise RuntimeError("Edge unavailable")
                return "chromium-context"

        browser = Browser()

        self.assertEqual(_launch_weibo_browser(browser, visible=False), "chromium-context")
        self.assertEqual(len(browser.calls), 2)
        self.assertNotIn("channel", browser.calls[1])

    def test_detects_weibo_login_page_text(self):
        text = "扫描二维码登录\n打开微博手机APP - 我的页面 - 扫一扫\n登录/注册"

        self.assertTrue(_is_weibo_login_page(text))

    def test_does_not_treat_api_json_as_login_page(self):
        self.assertFalse(_is_weibo_login_page('{"ok": 1, "data": {}}'))

    def test_login_html_is_routed_to_the_login_flow(self):
        text = "扫描二维码登录\n账号登录\n登录/注册"

        self.assertIsNone(_decode_weibo_api_response(text))

    def test_uses_page_number_when_since_id_is_absent_after_page_one(self):
        params = _build_weibo_feed_params("123", "107603123", page=2, since_id=None)

        self.assertEqual(params["page"], 2)
        self.assertNotIn("since_id", params)

    def test_prefers_since_id_when_the_api_provides_one(self):
        params = _build_weibo_feed_params("123", "107603123", page=2, since_id="next-token")

        self.assertEqual(params["since_id"], "next-token")
        self.assertNotIn("page", params)

    def test_ignores_non_dictionary_picture_entries(self):
        pictures = [{"pid": "valid"}, "not-a-picture", 42]

        self.assertEqual(_iter_weibo_picture_dicts(pictures), [{"pid": "valid"}])

    def test_accepts_single_picture_or_picture_mapping(self):
        single = {"pid": "single", "large": {"url": "https://example.invalid/a.jpg"}}
        mapping = {"first": {"pid": "one"}, "second": {"pid": "two"}}

        self.assertEqual(_iter_weibo_picture_dicts(single), [single])
        self.assertEqual(
            _iter_weibo_picture_dicts(mapping),
            [{"pid": "one"}, {"pid": "two"}],
        )

    def test_ignores_string_picture_values(self):
        self.assertEqual(_iter_weibo_picture_dicts("unexpected string"), [])

    def test_parses_full_status_embedded_in_mobile_detail_page(self):
        page = '<script>var $render_data = [{"status":{"id":"post-12","pic_num":12}}][0]</script>'

        self.assertEqual(_parse_weibo_detail_status(page), {"id": "post-12", "pic_num": 12})


class WeiboFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_time_range_keeps_only_posts_inside_inclusive_dates(self):
        provider = WeiboProvider()

        async def fake_api_get(_params):
            return {
                "ok": 1,
                "data": {
                    "cardlistInfo": {"page": 1},
                    "cards": [
                        {"mblog": {"id": "inside", "created_at": "Wed, 15 Feb 2023 12:00:00 +0800", "pics": [
                            {"pid": "one", "large": {"url": "https://example.invalid/inside.jpg"}},
                        ]}},
                        {"mblog": {"id": "outside", "created_at": "Wed, 01 Mar 2023 12:00:00 +0800", "pics": [
                            {"pid": "two", "large": {"url": "https://example.invalid/outside.jpg"}},
                        ]}},
                    ],
                },
            }

        provider._api_get = fake_api_get
        target = TargetInfo(provider="weibo", target_id="123", display_name="test", avatar_url="", extra={})
        pages = [
            page async for page in provider.iter_media_pages(
                target, start_date=date(2023, 2, 1), end_date=date(2023, 2, 28), end_page=1
            )
        ]

        self.assertEqual([item.source_id for item in pages[0][1]], ["inside_one"])

    async def test_api_requests_do_not_acquire_a_synchronous_event_loop_lock(self):
        class ForbiddenLock:
            def __enter__(self):
                raise AssertionError("同步锁会阻塞 FastAPI 事件循环")

            def __exit__(self, *_args):
                return False

        provider = WeiboProvider()
        with patch("weibo_support.provider._browser_lock", ForbiddenLock(), create=True), patch(
            "weibo_support.provider._sync_browser_request",
            return_value=({"ok": 1}, ""),
        ):
            result = await provider._api_get({"type": "uid", "value": "1"})

        self.assertEqual(result, {"ok": 1})

    async def test_fetch_following_prefers_since_id_cursor(self):
        provider = WeiboProvider()
        requests = []
        responses = iter([
            {"ok": 1, "data": {"cardlistInfo": {"has_more": 1, "since_id": "cursor-1"}, "cards": [
                {"user": {"id": "1", "screen_name": "甲"}},
            ]}},
            {"ok": 1, "data": {"cardlistInfo": {"has_more": 0}, "cards": [
                {"user": {"id": "2", "screen_name": "乙"}},
            ]}},
        ])

        async def fake_api_get(params):
            requests.append(dict(params))
            return next(responses)

        provider._api_get = fake_api_get
        provider._fetch_following_desktop = AsyncMock(return_value={"profiles": []})
        with patch("weibo_support.provider.asyncio.sleep", new=AsyncMock()):
            result = await provider.fetch_following()

        self.assertEqual([item["name"] for item in result["profiles"]], ["甲", "乙"])
        self.assertEqual(requests[1].get("since_id"), "cursor-1")
        self.assertNotIn("page", requests[1])

    async def test_fetch_following_stops_when_numbered_page_repeats(self):
        provider = WeiboProvider()
        calls = 0
        response = {"ok": 1, "data": {"cardlistInfo": {}, "cards": [
            {"user": {"id": "1", "screen_name": "甲"}},
        ]}}

        async def fake_api_get(_params):
            nonlocal calls
            calls += 1
            return response

        provider._api_get = fake_api_get
        provider._fetch_following_desktop = AsyncMock(return_value={"profiles": []})
        with patch("weibo_support.provider.asyncio.sleep", new=AsyncMock()):
            result = await provider.fetch_following()

        self.assertEqual([item["name"] for item in result["profiles"]], ["甲"])
        self.assertEqual(calls, 2)

    async def test_fetch_following_merges_pages_without_duplicates(self):
        provider = WeiboProvider()
        responses = iter([
            {"ok": 1, "data": {"cardlistInfo": {}, "cards": [
                {"card_group": [{"user": {"id": "1", "screen_name": "甲"}}, {"user": {"id": "2", "screen_name": "乙"}}]},
            ]}},
            {"ok": 1, "data": {"cardlistInfo": {"has_more": 0}, "cards": [
                {"user": {"id": "2", "screen_name": "乙"}},
                {"user": {"id": "3", "screen_name": "丙"}},
            ]}},
            {"ok": 1, "data": {"cardlistInfo": {}, "cards": []}},
        ])

        async def fake_api_get(_params):
            return next(responses)

        provider._api_get = fake_api_get
        provider._fetch_following_desktop = AsyncMock(return_value={"profiles": []})
        result = await provider.fetch_following()

        self.assertEqual([item["name"] for item in result["profiles"]], ["甲", "乙", "丙"])

    async def test_desktop_following_returns_displayed_and_readable_totals(self):
        provider = WeiboProvider()
        provider._cookie = "session=test"
        responses = iter([
            {"ok": 1, "data": {"user": {"friends_count": 3}}},
            {"ok": 1, "total_number": 2, "users": [
                {"idstr": "1", "screen_name": "甲"},
                {"idstr": "2", "screen_name": "乙"},
            ]},
        ])
        provider._api_url_get = AsyncMock(side_effect=lambda _url: next(responses))
        with patch("weibo_support.provider._sync_browser_current_uid", return_value=("99", "session=test")):
            result = await provider._fetch_following_desktop()

        self.assertEqual(result["reported_total"], 3)
        self.assertEqual(result["readable_total"], 2)
        self.assertEqual([item["name"] for item in result["profiles"]], ["甲", "乙"])

    def test_extracts_video_media_from_a_post(self):
        items = WeiboProvider._extract_media_items([{
            "mblog": {
                "id": "post-video",
                "page_info": {"media_info": {"stream_url_hd": "https://example.invalid/video.mp4"}},
            },
        }])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_id, "post-video_video")
        self.assertEqual(items[0].media_type.value, "video")

    def test_extracts_media_nested_in_card_group(self):
        items = WeiboProvider._extract_media_items([{
            "card_group": [{
                "mblog": {
                    "id": "nested-post",
                    "created_at": "Sat Aug 22 13:14:15 +0800 2026",
                    "text": "嵌套内容",
                    "pics": [{
                        "pid": "nested-picture",
                        "large": {"url": "https://example.invalid/nested.jpg"},
                    }],
                },
            }],
        }])

        self.assertEqual([item.source_id for item in items], ["nested-post_nested-picture"])
        self.assertEqual(items[0].filename, "01.jpg")
        self.assertTrue(items[0].extra["relative_dir"].startswith("2026-08-22_嵌套内容_nested-post"))

    def test_extracts_all_picture_infos_in_declared_post_order(self):
        picture_ids = [f"picture-{index:02d}" for index in range(1, 13)]
        items = WeiboProvider._extract_media_items([{
            "mblog": {
                "id": "many-pictures",
                "pic_num": 12,
                # Timeline cards can expose only the first nine images.
                "pics": [
                    {"pid": picture_id, "large": {"url": f"https://example.invalid/{picture_id}.jpg"}}
                    for picture_id in picture_ids[:9]
                ],
                # The full detail payload contains all image metadata.
                "pic_ids": picture_ids,
                "pic_infos": {
                    picture_id: {"largest": {"url": f"https://example.invalid/{picture_id}.jpg"}}
                    for picture_id in picture_ids
                },
            },
        }])

        self.assertEqual(len(items), 12)
        self.assertEqual([item.source_id for item in items], [
            f"many-pictures_{picture_id}" for picture_id in picture_ids
        ])
        self.assertEqual(items[-1].filename, "12.jpg")

    async def test_media_page_fetches_detail_when_timeline_has_only_nine_of_twelve_images(self):
        provider = WeiboProvider()
        picture_ids = [f"picture-{index:02d}" for index in range(1, 13)]

        async def fake_api_get(_params):
            return {
                "ok": 1,
                "data": {"cardlistInfo": {"page": 1}, "cards": [{"mblog": {
                    "id": "many-pictures", "pic_num": 12,
                    "pics": [
                        {"pid": picture_id, "large": {"url": f"https://example.invalid/{picture_id}.jpg"}}
                        for picture_id in picture_ids[:9]
                    ],
                }}]},
            }

        provider._api_get = fake_api_get
        provider._fetch_weibo_detail_mblog = AsyncMock(return_value={
            "id": "many-pictures", "pic_num": 12, "pic_ids": picture_ids,
            "pic_infos": {
                picture_id: {"largest": {"url": f"https://example.invalid/{picture_id}.jpg"}}
                for picture_id in picture_ids
            },
        })
        target = TargetInfo(
            provider="weibo", target_id="123", display_name="test", avatar_url="",
            extra={"container_id": "107603123"},
        )

        pages = [page async for page in provider.iter_media_pages(target, end_page=1)]

        self.assertEqual(len(pages[0][1]), 12)
        provider._fetch_weibo_detail_mblog.assert_awaited_once_with("many-pictures")

    def test_extracts_media_from_retweeted_post_into_outer_post_folder(self):
        items = WeiboProvider._extract_media_items([{
            "mblog": {
                "id": "outer-post",
                "created_at": "Sat Aug 22 13:14:15 +0800 2026",
                "text": "转发说明",
                "retweeted_status": {
                    "id": "original-post",
                    "pics": [{
                        "pid": "original-picture",
                        "large": {"url": "https://example.invalid/original.jpg"},
                    }],
                },
            },
        }])

        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0].source_id,
            "outer-post_retweet_original-post_original-picture",
        )
        self.assertIn("outer-post", items[0].extra["relative_dir"])

    async def test_latest_update_ids_include_text_only_weibo_posts(self):
        provider = WeiboProvider()

        async def fake_api_get(_params):
            return {
                "ok": 1,
                "data": {"cards": [
                    {"mblog": {"id": "text-post", "text": "只有文字"}},
                    {"mblog": {"id": "media-post", "pics": [{"pid": "image"}]}},
                    {"card_group": [{"mblog": {"id": "text-post", "text": "重复卡片"}}]},
                ]},
            }

        provider._api_get = fake_api_get
        target = TargetInfo(
            provider="weibo", target_id="123", display_name="test", avatar_url="",
            extra={"container_id": "107603123"},
        )

        self.assertEqual(await provider.get_latest_update_source_ids(target), ["text-post", "media-post"])

    async def test_update_check_ignores_downloaded_pinned_post_before_new_timeline_posts(self):
        provider = WeiboProvider()

        async def fake_api_get(_params):
            return {
                "ok": 1,
                "data": {"cards": [
                    {"mblog": {"id": "pinned-old", "isTop": 1, "text": "置顶旧微博"}},
                    {"mblog": {"id": "new-post", "text": "最新普通微博"}},
                    {"mblog": {"id": "download-boundary", "text": "最后成功下载的普通微博"}},
                ], "cardlistInfo": {}},
            }

        provider._api_get = fake_api_get
        target = TargetInfo(
            provider="weibo", target_id="123", display_name="test", avatar_url="",
            extra={"container_id": "107603123"},
        )

        new_ids, boundary_found = await provider.get_update_ids_after(
            target, {"pinned-old", "download-boundary"}
        )

        self.assertTrue(boundary_found)
        self.assertEqual(new_ids, ["new-post"])

    def test_live_photo_downloads_only_native_motion_file(self):
        items = WeiboProvider._extract_media_items([{
            "mblog": {
                "id": "live-post",
                "created_at": "Sat Aug 22 13:14:15 +0800 2026",
                "text": "原生 Live",
                "live_photo": ["live-token"],
                "pics": [{
                    "pid": "live-picture",
                    "type": "livephoto",
                    "videoSrc": "https://video.example.invalid/live-motion",
                    "large": {"url": "https://example.invalid/static-cover.jpg"},
                }],
            },
        }])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].media_type.value, "video")
        self.assertEqual(items[0].url, "https://video.example.invalid/live-motion")
        self.assertEqual(items[0].filename, "01.mov")
        self.assertEqual(items[0].source_id, "live-post_live-picture_live")

    async def test_fetch_skips_string_picture_entries(self):
        provider = WeiboProvider()

        async def fake_api_get(_params):
            return {
                "ok": 1,
                "data": {
                    "cardlistInfo": {"page": 1},
                    "cards": [{
                        "mblog": {
                            "id": "post-1",
                            "pics": [
                                {"pid": "valid", "large": {"url": "https://example.invalid/a.jpg"}},
                                "unexpected string",
                            ],
                        },
                    }],
                },
            }

        provider._api_get = fake_api_get
        target = TargetInfo(
            provider="weibo",
            target_id="123",
            display_name="test",
            avatar_url="",
            extra={"container_id": "107603123"},
        )

        items = await provider.fetch_media_list(target, end_page=1)

        self.assertEqual([item.source_id for item in items], ["post-1_valid"])

    async def test_page_stream_yields_each_page_before_completion(self):
        provider = WeiboProvider()
        responses = iter([
            {
                "ok": 1,
                "data": {
                    "cardlistInfo": {"page": 1},
                    "cards": [{"mblog": {"id": "post-1", "pics": [
                        {"pid": "one", "large": {"url": "https://example.invalid/1.jpg"}},
                    ]}}],
                },
            },
            {"ok": 1, "data": {"cardlistInfo": {"page": 2}, "cards": []}},
            {"ok": 1, "data": {"cardlistInfo": {"page": 2}, "cards": []}},
        ])

        async def fake_api_get(_params):
            return next(responses)

        provider._api_get = fake_api_get
        target = TargetInfo(
            provider="weibo", target_id="123", display_name="test", avatar_url="",
            extra={"container_id": "107603123"},
        )

        pages = [
            page async for page in provider.iter_media_pages(target, start_page=1, end_page=2)
        ]

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0][0], 1)
        self.assertEqual([item.source_id for item in pages[0][1]], ["post-1_one"])

    async def test_empty_page_without_end_marker_is_confirmed_before_completion(self):
        provider = WeiboProvider()
        requests = []
        responses = iter([
            {"ok": 1, "data": {"cardlistInfo": {"page": 1}, "cards": [{"mblog": {
                "id": "post-1", "pics": [{"pid": "one", "large": {"url": "https://example.invalid/1.jpg"}}],
            }}]}},
            {"ok": 1, "data": {"cardlistInfo": {"page": 2}, "cards": []}},
            {"ok": 1, "data": {"cardlistInfo": {"page": 2}, "cards": []}},
        ])

        async def fake_api_get(params):
            requests.append(dict(params))
            return next(responses)

        provider._api_get = fake_api_get
        target = TargetInfo(provider="weibo", target_id="123", display_name="test", avatar_url="", extra={"container_id": "107603123"})
        pages = [page async for page in provider.iter_media_pages(target)]

        self.assertEqual(len(pages), 1)
        self.assertEqual([request.get("page", 1) for request in requests], [1, 2, 2])

    async def test_empty_page_confirmation_with_a_non_success_response_remains_an_error(self):
        provider = WeiboProvider()
        responses = iter([
            {"ok": 1, "data": {"cardlistInfo": {"page": 1}, "cards": [{"mblog": {
                "id": "post-1", "pics": [{"pid": "one", "large": {"url": "https://example.invalid/1.jpg"}}],
            }}]}},
            {"ok": 1, "data": {"cardlistInfo": {"page": 2}, "cards": []}},
            {"ok": 0, "msg": "这里还没有内容"},
        ])

        async def fake_api_get(_params):
            return next(responses)

        provider._api_get = fake_api_get
        target = TargetInfo(provider="weibo", target_id="123", display_name="test", avatar_url="", extra={"container_id": "107603123"})

        with self.assertRaisesRegex(ValueError, "第 2 页读取异常.*这里还没有内容"):
            [page async for page in provider.iter_media_pages(target)]


if __name__ == "__main__":
    unittest.main()

