import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiosqlite


class Database:
    def __init__(self, db_path: str = "dy_downloader.db"):
        self.db_path = db_path
        self._initialized = False
        self._conn: Optional[aiosqlite.Connection] = None
        # 延迟到首次 _get_conn 调用时在当前 event loop 上创建 Lock，
        # 避免在 __init__ 阶段抢到错误的 loop。
        self._conn_lock: Optional[asyncio.Lock] = None

    async def _get_conn(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()
        async with self._conn_lock:
            if self._conn is None:
                self._conn = await aiosqlite.connect(self.db_path)
        return self._conn

    async def initialize(self):
        if self._initialized:
            return

        db = await self._get_conn()

        # WAL gives concurrent reader/writer; NORMAL avoids fsync on every commit
        # (loses at most last few txns on power loss — acceptable for download history).
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS aweme (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                aweme_id TEXT UNIQUE NOT NULL,
                aweme_type TEXT NOT NULL,
                title TEXT,
                author_id TEXT,
                author_name TEXT,
                create_time INTEGER,
                download_time INTEGER,
                file_path TEXT,
                metadata TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS download_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                url_type TEXT NOT NULL,
                download_time INTEGER,
                total_count INTEGER,
                success_count INTEGER,
                config TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS transcript_job (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                aweme_id TEXT NOT NULL,
                video_path TEXT NOT NULL,
                transcript_dir TEXT,
                text_path TEXT,
                json_path TEXT,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                skip_reason TEXT,
                error_message TEXT,
                created_at INTEGER,
                updated_at INTEGER,
                UNIQUE(aweme_id, video_path, model)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS download_failure (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                aweme_id TEXT NOT NULL,
                author_sec_uid TEXT,
                author_name TEXT,
                title TEXT,
                create_time INTEGER,
                failed_at INTEGER NOT NULL,
                reason TEXT NOT NULL
            )
        """)

        # Person-presence decisions are scoped to the profile, work and media
        # asset.  This keeps a batch of several homepage downloads isolated:
        # a cached decision for one person can never be reused for another.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS media_detection (
                author_sec_uid TEXT NOT NULL,
                aweme_id TEXT NOT NULL,
                asset_key TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT,
                model_version TEXT NOT NULL,
                sample_count INTEGER NOT NULL DEFAULT 0,
                detected_at INTEGER NOT NULL,
                PRIMARY KEY (author_sec_uid, aweme_id, asset_key)
            )
        """)

        # `job` persists the task-center JobManager records so they survive
        # a sidecar restart. Only terminal jobs (success / failed / cancelled)
        # are ever written here — see server/jobs.py. `last_retry_summary`
        # and `overrides` are stored as JSON text.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS job (
                job_id              TEXT PRIMARY KEY,
                url                 TEXT NOT NULL,
                status              TEXT NOT NULL,
                created_at          TEXT NOT NULL,
                started_at          TEXT,
                finished_at         TEXT,
                total               INTEGER NOT NULL DEFAULT 0,
                success             INTEGER NOT NULL DEFAULT 0,
                failed              INTEGER NOT NULL DEFAULT 0,
                skipped             INTEGER NOT NULL DEFAULT 0,
                error               TEXT,
                author_nickname     TEXT,
                author_sec_uid      TEXT,
                retry_count         INTEGER NOT NULL DEFAULT 0,
                last_retry_at       TEXT,
                last_retry_summary  TEXT,
                retry_history       TEXT,
                overrides           TEXT
            )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_aweme_id ON aweme(aweme_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_author_id ON aweme(author_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_download_time ON aweme(download_time)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_transcript_aweme_id ON transcript_job(aweme_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_transcript_status ON transcript_job(status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_failure_author ON download_failure(author_sec_uid)"
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_job_created_at ON job(created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_job_status ON job(status)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_detection_aweme ON media_detection(aweme_id)"
        )

        # Incremental migration: add author_sec_uid column to legacy aweme tables.
        # Running initialize() twice must be a no-op.
        cursor = await db.execute("PRAGMA table_info(aweme)")
        existing_columns = {row[1] for row in await cursor.fetchall()}
        if "author_sec_uid" not in existing_columns:
            await db.execute("ALTER TABLE aweme ADD COLUMN author_sec_uid TEXT")

        # Incremental migration: add retry_history column to legacy job
        # tables so pre-existing DB files (created before retry-history
        # persistence landed) continue to work. NULL for old rows; the
        # Restore paths map NULL -> [] so API consumers receive
        # no history for those jobs.
        cursor = await db.execute("PRAGMA table_info(job)")
        existing_job_columns = {row[1] for row in await cursor.fetchall()}
        if "retry_history" not in existing_job_columns:
            await db.execute("ALTER TABLE job ADD COLUMN retry_history TEXT")

        await db.commit()
        self._initialized = True

    async def is_downloaded(self, aweme_id: str) -> bool:
        db = await self._get_conn()
        cursor = await db.execute("SELECT id FROM aweme WHERE aweme_id = ?", (aweme_id,))
        result = await cursor.fetchone()
        return result is not None

    async def add_aweme(
        self,
        aweme_data: Dict[str, Any],
        *,
        author_sec_uid: Optional[str] = None,
    ):
        db = await self._get_conn()
        # Prefer the explicit kwarg; fall back to a key on the payload so existing
        # callers (tests, legacy downloaders) keep working.
        sec_uid = author_sec_uid if author_sec_uid is not None else aweme_data.get("author_sec_uid")
        await db.execute(
            """
            INSERT OR REPLACE INTO aweme
            (aweme_id, aweme_type, title, author_id, author_name, author_sec_uid,
             create_time, download_time, file_path, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                aweme_data.get("aweme_id"),
                aweme_data.get("aweme_type"),
                aweme_data.get("title"),
                aweme_data.get("author_id"),
                aweme_data.get("author_name"),
                sec_uid,
                aweme_data.get("create_time"),
                int(datetime.now().timestamp()),
                aweme_data.get("file_path"),
                aweme_data.get("metadata"),
            ),
        )
        await db.commit()

    async def add_aweme_batch(self, items: List[Dict[str, Any]]) -> None:
        """Insert N awemes in a single transaction. Replaces existing rows by aweme_id."""
        if not items:
            return
        db = await self._get_conn()
        now_ts = int(datetime.now().timestamp())
        rows = [
            (
                item.get("aweme_id"),
                item.get("aweme_type"),
                item.get("title"),
                item.get("author_id"),
                item.get("author_name"),
                item.get("author_sec_uid"),
                item.get("create_time"),
                now_ts,
                item.get("file_path"),
                item.get("metadata"),
            )
            for item in items
        ]
        await db.executemany(
            """
            INSERT OR REPLACE INTO aweme
            (aweme_id, aweme_type, title, author_id, author_name, author_sec_uid,
             create_time, download_time, file_path, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            rows,
        )
        await db.commit()

    async def get_latest_aweme_time(self, author_id: str) -> Optional[int]:
        db = await self._get_conn()
        cursor = await db.execute(
            "SELECT MAX(create_time) FROM aweme WHERE author_id = ?", (author_id,)
        )
        result = await cursor.fetchone()
        return result[0] if result and result[0] else None

    async def add_history(self, history_data: Dict[str, Any]):
        db = await self._get_conn()
        await db.execute(
            """
            INSERT INTO download_history
            (url, url_type, download_time, total_count, success_count, config)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                history_data.get("url"),
                history_data.get("url_type"),
                int(datetime.now().timestamp()),
                history_data.get("total_count"),
                history_data.get("success_count"),
                history_data.get("config"),
            ),
        )
        await db.commit()

    async def get_aweme_history(
        self,
        *,
        page: int = 1,
        size: int = 50,
        author: Optional[str] = None,
        date_from: Optional[int] = None,
        date_to: Optional[int] = None,
        aweme_type: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Paginated aweme history, newest download first.

        `date_from` / `date_to` are unix-seconds (filter against `create_time`).
        `aweme_type` matches the `aweme_type` column (e.g. 'video', 'gallery').
        `title` is a case-insensitive substring match on the title column.
        """
        db = await self._get_conn()
        where: list = []
        params: list = []
        if author:
            where.append("author_name = ?")
            params.append(author)
        if date_from is not None:
            where.append("create_time >= ?")
            params.append(int(date_from))
        if date_to is not None:
            where.append("create_time <= ?")
            params.append(int(date_to))
        if aweme_type:
            where.append("aweme_type = ?")
            params.append(aweme_type)
        if title:
            where.append("LOWER(COALESCE(title, '')) LIKE ?")
            params.append(f"%{title.lower()}%")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        cursor = await db.execute(f"SELECT COUNT(*) FROM aweme {where_sql}", params)
        row = await cursor.fetchone()
        total = int(row[0]) if row else 0

        offset = max(0, (page - 1) * size)
        cursor = await db.execute(
            f"SELECT aweme_id, aweme_type, title, author_id, author_name, "
            f"author_sec_uid, create_time, download_time, file_path FROM aweme "
            f"{where_sql} ORDER BY download_time DESC, id DESC LIMIT ? OFFSET ?",
            params + [int(size), int(offset)],
        )
        rows = await cursor.fetchall()
        items = [
            {
                "aweme_id": r[0],
                "aweme_type": r[1],
                "title": r[2],
                "author_id": r[3],
                "author_name": r[4],
                "author_sec_uid": r[5],
                "create_time": r[6],
                "download_time": r[7],
                "file_path": r[8],
            }
            for r in rows
        ]
        return {"total": total, "page": int(page), "size": int(size), "items": items}

    async def get_aweme_count_by_author(self, author_id: str) -> int:
        db = await self._get_conn()
        cursor = await db.execute("SELECT COUNT(*) FROM aweme WHERE author_id = ?", (author_id,))
        result = await cursor.fetchone()
        return result[0] if result else 0

    async def get_person_download_summary(
        self, author_sec_uid: str, url: str = "", author_name: str = ""
    ) -> Dict[str, int]:
        """Return the persisted media/history totals for one profile without exposing rows."""
        db = await self._get_conn()
        cursor = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN file_path IS NOT NULL AND file_path != '' THEN 1 ELSE 0 END), 0) "
            "FROM aweme WHERE author_sec_uid = ?",
            (author_sec_uid,),
        )
        row = await cursor.fetchone()
        # Old records may not have a compatible sec_uid. Name fallback is read-only
        # dashboard data; destructive clearing remains sec_uid-only.
        if (not row or not row[0]) and author_name:
            cursor = await db.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN file_path IS NOT NULL AND file_path != '' THEN 1 ELSE 0 END), 0) "
                "FROM aweme WHERE author_name = ?",
                (author_name,),
            )
            row = await cursor.fetchone()
        history_count = 0
        failed_count = 0
        if url:
            cursor = await db.execute("SELECT COUNT(*) FROM download_history WHERE url = ?", (url,))
            history = await cursor.fetchone()
            history_count = int(history[0]) if history else 0
            cursor = await db.execute("SELECT COALESCE(SUM(failed), 0) FROM job WHERE url = ?", (url,))
            failed = await cursor.fetchone()
            failed_count = int(failed[0]) if failed else 0
        return {
            "downloaded_count": int(row[0]) if row else 0,
            "file_count": int(row[1]) if row else 0,
            "download_count": history_count,
            "failed_count": failed_count,
        }

    async def get_successful_person_aweme_ids(
        self, author_sec_uid: str, author_name: str = ""
    ) -> set[str]:
        """Return only works with a persisted local file for update-boundary checks."""
        db = await self._get_conn()
        cursor = await db.execute(
            "SELECT aweme_id FROM aweme WHERE author_sec_uid = ? "
            "AND file_path IS NOT NULL AND file_path != ''",
            (author_sec_uid,),
        )
        rows = await cursor.fetchall()
        if not rows and author_name:
            cursor = await db.execute(
                "SELECT aweme_id FROM aweme WHERE author_name = ? "
                "AND file_path IS NOT NULL AND file_path != ''",
                (author_name,),
            )
            rows = await cursor.fetchall()
        return {str(row[0]) for row in rows if row and row[0]}

    async def clear_person_download_data(self, author_sec_uid: str, url: str = "") -> Dict[str, int]:
        """Remove only one profile's deduplication/history records, never local files."""
        db = await self._get_conn()
        cursor = await db.execute("DELETE FROM aweme WHERE author_sec_uid = ?", (author_sec_uid,))
        aweme_deleted = int(cursor.rowcount or 0)
        cursor = await db.execute(
            "DELETE FROM media_detection WHERE author_sec_uid = ?", (author_sec_uid,)
        )
        detection_deleted = int(cursor.rowcount or 0)
        history_deleted = 0
        if url:
            cursor = await db.execute("DELETE FROM download_history WHERE url = ?", (url,))
            history_deleted = int(cursor.rowcount or 0)
        await db.commit()
        return {
            "aweme_deleted": aweme_deleted,
            "history_deleted": history_deleted,
            "detection_deleted": detection_deleted,
        }

    async def get_media_detection(
        self,
        author_sec_uid: str,
        aweme_id: str,
        asset_key: str,
        model_version: str,
    ) -> Optional[Dict[str, Any]]:
        """Fetch a reusable media decision for one exact profile asset."""
        db = await self._get_conn()
        cursor = await db.execute(
            """
            SELECT decision, reason, model_version, sample_count
            FROM media_detection
            WHERE author_sec_uid = ? AND aweme_id = ? AND asset_key = ? AND model_version = ?
            """,
            (author_sec_uid, aweme_id, asset_key, model_version),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "decision": row[0],
            "reason": row[1] or "",
            "model_version": row[2],
            "sample_count": int(row[3] or 0),
        }

    async def upsert_media_detection(
        self,
        author_sec_uid: str,
        aweme_id: str,
        asset_key: str,
        decision: str,
        reason: str,
        model_version: str,
        sample_count: int,
    ) -> None:
        db = await self._get_conn()
        await db.execute(
            """
            INSERT OR REPLACE INTO media_detection
            (author_sec_uid, aweme_id, asset_key, decision, reason, model_version, sample_count, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                author_sec_uid,
                aweme_id,
                asset_key,
                decision,
                reason,
                model_version,
                max(0, int(sample_count or 0)),
                int(datetime.now().timestamp()),
            ),
        )
        await db.commit()

    async def add_failure_batch(self, items: List[Dict[str, Any]]) -> None:
        """Persist failed item details so the desktop can show them later."""
        if not items:
            return
        db = await self._get_conn()
        now_ts = int(datetime.now().timestamp())
        await db.executemany(
            """
            INSERT INTO download_failure
            (aweme_id, author_sec_uid, author_name, title, create_time, failed_at, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(item.get("aweme_id") or "unknown"),
                    item.get("author_sec_uid"),
                    item.get("author_name"),
                    item.get("title"),
                    item.get("create_time"),
                    now_ts,
                    str(item.get("reason") or "下载失败"),
                )
                for item in items
            ],
        )
        await db.commit()

    async def get_download_failures(
        self, author_sec_uid: Optional[str] = None, author_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        db = await self._get_conn()
        clauses = []
        params: List[Any] = []
        if author_sec_uid:
            clauses.append("author_sec_uid = ?")
            params.append(author_sec_uid)
        elif author_name:
            clauses.append("author_name = ?")
            params.append(author_name)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = await db.execute(
            f"SELECT aweme_id, author_name, title, create_time, failed_at, reason "
            f"FROM download_failure {where} ORDER BY failed_at DESC, id DESC",
            params,
        )
        rows = await cursor.fetchall()
        return [
            {
                "aweme_id": row[0],
                "author_name": row[1] or "",
                "title": row[2] or "",
                "create_time": row[3] or 0,
                "failed_at": row[4] or 0,
                "reason": row[5] or "下载失败",
            }
            for row in rows
        ]
    async def get_top_authors(self, *, days: int, limit: int) -> List[Dict[str, Any]]:
        """Return the most-downloaded authors in the last ``days`` days.

        Aggregates rows in `aweme` with ``create_time >= now - days*86400`` and
        non-empty / non-null ``author_sec_uid``. Groups by ``author_sec_uid``
        and orders by ``COUNT(*) DESC, author_sec_uid ASC`` (stable tie-break
        so property tests are deterministic). Truncates to ``limit`` rows.

        ``author_name`` for each result row is the latest non-empty
        ``author_name`` for that ``sec_uid`` (ordered by ``download_time``
        descending). If all rows for that sec_uid have empty/null names,
        falls back to the Chinese placeholder ``"未知作者"``.

        Each returned dict contains ``sec_uid`` / ``author_name`` /
        ``download_count``.
        """
        cutoff = int(datetime.now().timestamp()) - int(days) * 86400
        db = await self._get_conn()
        cursor = await db.execute(
            """
            SELECT a.author_sec_uid,
                   (SELECT a2.author_name FROM aweme a2
                     WHERE a2.author_sec_uid = a.author_sec_uid
                       AND a2.author_name IS NOT NULL
                       AND a2.author_name != ''
                     ORDER BY a2.download_time DESC
                     LIMIT 1) AS author_name,
                   COUNT(*) AS download_count
              FROM aweme a
             WHERE a.create_time >= ?
               AND a.author_sec_uid IS NOT NULL
               AND a.author_sec_uid != ''
             GROUP BY a.author_sec_uid
             ORDER BY download_count DESC, a.author_sec_uid ASC
             LIMIT ?
            """,
            (cutoff, int(limit)),
        )
        rows = await cursor.fetchall()
        return [
            {
                "sec_uid": row[0],
                "author_name": row[1] if row[1] else "未知作者",
                "download_count": int(row[2]),
            }
            for row in rows
        ]

    async def upsert_transcript_job(self, job_data: Dict[str, Any]):
        now_ts = int(datetime.now().timestamp())
        db = await self._get_conn()
        await db.execute(
            """
            INSERT INTO transcript_job (
                aweme_id,
                video_path,
                transcript_dir,
                text_path,
                json_path,
                model,
                status,
                skip_reason,
                error_message,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(aweme_id, video_path, model) DO UPDATE SET
                transcript_dir = excluded.transcript_dir,
                text_path = excluded.text_path,
                json_path = excluded.json_path,
                status = excluded.status,
                skip_reason = excluded.skip_reason,
                error_message = excluded.error_message,
                updated_at = excluded.updated_at
        """,
            (
                job_data.get("aweme_id"),
                job_data.get("video_path"),
                job_data.get("transcript_dir"),
                job_data.get("text_path"),
                job_data.get("json_path"),
                job_data.get("model") or "gpt-4o-mini-transcribe",
                job_data.get("status"),
                job_data.get("skip_reason"),
                job_data.get("error_message"),
                now_ts,
                now_ts,
            ),
        )
        await db.commit()

    async def get_transcript_job(self, aweme_id: str) -> Optional[Dict[str, Any]]:
        db = await self._get_conn()
        cursor = await db.execute(
            """
            SELECT aweme_id, video_path, transcript_dir, text_path, json_path,
                   model, status, skip_reason, error_message, created_at, updated_at
            FROM transcript_job
            WHERE aweme_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (aweme_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "aweme_id": row[0],
            "video_path": row[1],
            "transcript_dir": row[2],
            "text_path": row[3],
            "json_path": row[4],
            "model": row[5],
            "status": row[6],
            "skip_reason": row[7],
            "error_message": row[8],
            "created_at": row[9],
            "updated_at": row[10],
        }

    async def delete_aweme_by_ids(self, aweme_ids: List[str]) -> int:
        """Delete aweme rows by their string id. Returns the number of rows removed.

        Empty input is a no-op that returns 0 without issuing any SQL.

        Uses a parameterized ``DELETE ... WHERE aweme_id IN (?,?,...)`` statement
        because ``aiosqlite.Cursor.rowcount`` is not reliably populated after
        ``executemany`` across all versions. Chunked at 500 ids per statement to
        stay well below SQLite's host-parameter limit (historically 999).
        """
        if not aweme_ids:
            return 0
        # De-duplicate input while preserving a stable order. Duplicate ids would
        # otherwise match the same row twice in different chunks and inflate the
        # returned count beyond the rows actually affected.
        seen: Dict[str, None] = {}
        for aid in aweme_ids:
            if aid not in seen:
                seen[aid] = None
        unique_ids = list(seen.keys())

        db = await self._get_conn()
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()
        deleted = 0
        chunk_size = 500
        async with self._conn_lock:
            for start in range(0, len(unique_ids), chunk_size):
                chunk = unique_ids[start : start + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                cursor = await db.execute(
                    f"DELETE FROM aweme WHERE aweme_id IN ({placeholders})",
                    chunk,
                )
                if cursor.rowcount is not None and cursor.rowcount > 0:
                    deleted += cursor.rowcount
            await db.commit()
        return deleted

    async def truncate_history(self) -> None:
        """Delete every row from `aweme` and `download_history`.

        Does not touch disk files or any other table (e.g. transcript_job).
        """
        db = await self._get_conn()
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()
        async with self._conn_lock:
            await db.execute("DELETE FROM aweme")
            await db.execute("DELETE FROM download_history")
            await db.commit()

    # ------------------------------------------------------------------
    # Task-center job persistence (see server/jobs.py)
    # ------------------------------------------------------------------

    async def upsert_job(self, job_dict: Dict[str, Any]) -> None:
        """Insert or replace a task-center job record.

        Accepts the dict produced by :py:meth:`server.jobs.DownloadJob.to_dict`
        plus an optional ``overrides`` key (the JobManager stores overrides
        separately on the in-memory job but we persist them too so future
        retries/re-runs can inherit them). Unknown keys are ignored — any
        client-only computed fields (``url_type``, ``duration_ms`` etc.)
        are recomputed from raw columns on read.
        """
        db = await self._get_conn()
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()

        last_retry_summary = job_dict.get("last_retry_summary")
        retry_history = job_dict.get("retry_history")
        overrides = job_dict.get("overrides")
        params = (
            job_dict.get("job_id"),
            job_dict.get("url") or "",
            job_dict.get("status") or "",
            job_dict.get("created_at") or "",
            job_dict.get("started_at"),
            job_dict.get("finished_at"),
            int(job_dict.get("total") or 0),
            int(job_dict.get("success") or 0),
            int(job_dict.get("failed") or 0),
            int(job_dict.get("skipped") or 0),
            job_dict.get("error"),
            job_dict.get("author_nickname"),
            job_dict.get("author_sec_uid"),
            int(job_dict.get("retry_count") or 0),
            job_dict.get("last_retry_at"),
            json.dumps(last_retry_summary) if last_retry_summary else None,
            json.dumps(retry_history) if retry_history else None,
            json.dumps(overrides) if overrides else None,
        )
        async with self._conn_lock:
            await db.execute(
                """
                INSERT OR REPLACE INTO job (
                    job_id, url, status, created_at, started_at, finished_at,
                    total, success, failed, skipped, error,
                    author_nickname, author_sec_uid,
                    retry_count, last_retry_at, last_retry_summary,
                    retry_history, overrides
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            await db.commit()

    async def delete_jobs(self, job_ids: List[str]) -> int:
        """Delete job rows by id. Returns the number of rows deleted."""
        if not job_ids:
            return 0
        seen: Dict[str, None] = {}
        for jid in job_ids:
            if jid and jid not in seen:
                seen[jid] = None
        unique_ids = list(seen.keys())
        if not unique_ids:
            return 0

        db = await self._get_conn()
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()
        deleted = 0
        chunk_size = 500
        async with self._conn_lock:
            for start in range(0, len(unique_ids), chunk_size):
                chunk = unique_ids[start : start + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                cursor = await db.execute(
                    f"DELETE FROM job WHERE job_id IN ({placeholders})",
                    chunk,
                )
                if cursor.rowcount is not None and cursor.rowcount > 0:
                    deleted += cursor.rowcount
            await db.commit()
        return deleted

    async def load_terminal_jobs(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Load persisted terminal jobs ordered by created_at DESC.

        Only rows whose ``status`` is a terminal value (success / failed /
        cancelled) are returned. Running/pending rows shouldn't exist on
        disk — see server/jobs.py — but we filter defensively in case an
        older build left stale rows.
        """
        db = await self._get_conn()
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()

        sql = (
            "SELECT job_id, url, status, created_at, started_at, finished_at, "
            "total, success, failed, skipped, error, author_nickname, "
            "author_sec_uid, retry_count, last_retry_at, last_retry_summary, "
            "retry_history, overrides FROM job "
            "WHERE status IN ('success', 'failed', 'cancelled') "
            "ORDER BY created_at DESC"
        )
        if limit is not None and limit > 0:
            sql += f" LIMIT {int(limit)}"

        async with self._conn_lock:
            cursor = await db.execute(sql)
            rows = await cursor.fetchall()

        result: List[Dict[str, Any]] = []
        for row in rows:
            summary_raw = row[15]
            history_raw = row[16]
            overrides_raw = row[17]
            try:
                summary = json.loads(summary_raw) if summary_raw else None
            except (TypeError, ValueError):
                summary = None
            try:
                history = json.loads(history_raw) if history_raw else []
                if not isinstance(history, list):
                    history = []
            except (TypeError, ValueError):
                history = []
            try:
                overrides = json.loads(overrides_raw) if overrides_raw else None
            except (TypeError, ValueError):
                overrides = None
            result.append(
                {
                    "job_id": row[0],
                    "url": row[1],
                    "status": row[2],
                    "created_at": row[3],
                    "started_at": row[4],
                    "finished_at": row[5],
                    "total": row[6] or 0,
                    "success": row[7] or 0,
                    "failed": row[8] or 0,
                    "skipped": row[9] or 0,
                    "error": row[10],
                    "author_nickname": row[11],
                    "author_sec_uid": row[12],
                    "retry_count": row[13] or 0,
                    "last_retry_at": row[14],
                    "last_retry_summary": summary,
                    "retry_history": history,
                    "overrides": overrides,
                }
            )
        return result

    async def close(self):
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
