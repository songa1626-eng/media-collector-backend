from __future__ import annotations

from typing import Any, Dict, List

from core.user_modes.base_strategy import BaseUserModeStrategy
from utils.logger import setup_logger

logger = setup_logger("PostUserModeStrategy")


class PostUserModeStrategy(BaseUserModeStrategy):
    mode_name = "post"
    api_method_name = "get_user_post"

    @staticmethod
    def _chronological_key(item: Dict[str, Any]):
        """Sort posts by publish time so pinned posts cannot jump the queue."""
        raw_time = item.get("create_time") if isinstance(item, dict) else 0
        try:
            timestamp = int(raw_time or 0)
        except (TypeError, ValueError):
            timestamp = 0
        # Unknown timestamps are kept at the end and aweme_id makes ties stable.
        return (timestamp <= 0, timestamp, str(item.get("aweme_id") or ""))

    @staticmethod
    def _aweme_count(items: List[Dict[str, Any]]) -> int:
        return len({str(item.get("aweme_id") or "") for item in items if item.get("aweme_id")})

    def _is_full_profile_scope(self, number_limit: int) -> bool:
        if number_limit > 0:
            return False
        return not (
            self.downloader.config.get("start_time")
            or self.downloader.config.get("end_time")
        )

    @staticmethod
    def _profile_aweme_count(user_info: Dict[str, Any]) -> int:
        try:
            return max(0, int((user_info or {}).get("aweme_count") or 0))
        except (TypeError, ValueError):
            return 0

    def _set_unavailable_notice(self, source_total: int, source_collected: int) -> None:
        unavailable = max(0, source_total - source_collected)
        self._collection_source_total = max(0, source_total)
        self._collection_source_collected = max(0, source_collected)
        self._collection_unavailable_count = unavailable
        if unavailable:
            self._collection_unavailable_reason = (
                f"主页标称 {source_total} 条作品，本次只从抖音取得 {source_collected} 条；"
                f"其余 {unavailable} 条未由平台返回，未执行下载。可能为仅自己可见、隐私设置、"
                "审核状态或平台接口延迟。"
            )
            existing_warning = getattr(self, "_collection_warning", "")
            if existing_warning:
                self._collection_warning = (
                    f"{existing_warning}；{self._collection_unavailable_reason}"
                )
            else:
                self._collection_warning = self._collection_unavailable_reason

    async def collect_items(self, sec_uid: str, user_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        fetcher = getattr(self.downloader.api_client, self.api_method_name, None)
        if not callable(fetcher):
            logger.error("API client missing get_user_post")
            return []

        aweme_list: List[Dict[str, Any]] = []
        max_cursor = 0
        has_more = True
        pagination_restricted = False

        number_limit = int(self.downloader.config.get("number", {}).get(self.mode_name, 0) or 0)
        media_filter_enabled = self._media_type_filter_enabled()

        self.downloader._progress_update_step("拉取作品列表", "分页抓取中")

        while has_more:
            await self.downloader.rate_limiter.acquire()
            request_cursor = max_cursor
            page_data = await fetcher(sec_uid, request_cursor, 20)
            page = self._normalize_page_data(page_data)
            page_items = self.select_items(page)

            if not page_items:
                if page.get("status_code") == 0:
                    pagination_restricted = True
                    logger.warning(
                        "User post page empty at cursor=%s (status_code=0); "
                        "will attempt browser fallback",
                        request_cursor,
                    )
                break

            page_items = self._filter_pinned_items(page_items)
            aweme_list.extend(page_items)

            self.downloader._progress_update_step("拉取作品列表", f"已抓取 {len(aweme_list)} 条")

            has_more = bool(page.get("has_more", False))
            max_cursor = int(page.get("max_cursor", 0) or 0)
            if has_more and max_cursor == request_cursor:
                logger.warning(
                    "max_cursor did not advance (%s), stop paging to avoid loop",
                    max_cursor,
                )
                pagination_restricted = True
                break

            if number_limit > 0:
                if media_filter_enabled:
                    if len(self._filter_by_media_type(aweme_list)) >= number_limit:
                        break
                elif len(aweme_list) >= number_limit:
                    aweme_list = aweme_list[:number_limit]
                    break

        if pagination_restricted:
            self.downloader._progress_update_step("拉取作品列表", "分页受限，尝试浏览器回补")
            before_recovery = len(aweme_list)
            await self.downloader._recover_user_post_with_browser(sec_uid, user_info, aweme_list)
            if not aweme_list:
                raise RuntimeError(
                    "抖音接口未返回作品列表（可能触发了反爬限制），"
                    "请稍后重试或尝试重新登录抖音刷新 Cookie"
                )
            recovered_count = max(0, len(aweme_list) - before_recovery)
            self._collection_warning = (
                "抖音作品分页触发风控，已通过浏览器回补 "
                f"{recovered_count} 条；本次结果可能未完整，建议稍后重新更新以补全后续作品。"
            )

        source_collected = self._aweme_count(aweme_list)
        source_total = self._profile_aweme_count(user_info)
        full_profile_scope = self._is_full_profile_scope(number_limit)
        verify_completeness = bool(self.downloader.config.get("verify_profile_completeness", False))
        should_reconcile = full_profile_scope and (
            source_total > source_collected or verify_completeness
        )
        if should_reconcile and not pagination_restricted:
            self.downloader._progress_update_step("核验作品完整性", "正在与主页可见作品核对")
            recovery = await self.downloader._recover_user_post_with_browser(
                sec_uid,
                user_info,
                aweme_list,
                expected_count=source_total,
            )
            if isinstance(recovery, dict):
                browser_total = int(recovery.get("source_total") or 0)
                if browser_total:
                    source_total = max(source_total, browser_total)

        source_collected = self._aweme_count(aweme_list)
        if full_profile_scope:
            if source_total:
                self._set_unavailable_notice(source_total, source_collected)
            else:
                # The profile metadata endpoint can be blocked while the post
                # feed remains readable.  Report exactly what was returned,
                # but do not invent a missing-work count.
                self._collection_source_collected = source_collected

        # Douyin may return pinned posts before the normal feed regardless of
        # their publish time.  Download the complete selected set oldest-first
        # so pinned and regular posts follow the same chronological order.
        aweme_list.sort(key=self._chronological_key)
        return aweme_list
