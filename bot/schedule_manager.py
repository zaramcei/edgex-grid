"""
Schedule Manager
スケジュールに基づいてBot稼働を制御するマネージャー
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

import httpx
from loguru import logger


SCHEDULE_URL = "https://zaramcei.github.io/edgex-grid/schedule/schedule.json"
FETCH_INTERVAL_SEC = 300  # 5分

# スケジュールタイプ（環境変数から取得、デフォルトは "normal"）
SCHEDULE_TYPE = os.environ.get("EDGEX_USE_SCHEDULE_TYPE", "normal")


class ScheduleManager:
    """スケジュールに基づいてBot稼働を制御するマネージャー

    - 5分に1回リモートからスケジュールJSONを取得
    - 現在時刻がスケジュール内かどうかを判定
    - lot_coefficientを提供
    """

    def __init__(self) -> None:
        self._schedules: List[Dict[str, Any]] = []
        self._last_fetch_ts: float = 0.0
        self._fetch_lock = asyncio.Lock()
        self._schedule_type: str = SCHEDULE_TYPE
        logger.info("スケジュールタイプ: {}", self._schedule_type)

    async def fetch_schedule(self, force: bool = False) -> bool:
        """スケジュールJSONを取得・更新

        Args:
            force: Trueの場合、インターバルに関係なく強制取得

        Returns:
            bool: 取得成功したらTrue
        """
        now = time.time()

        # 前回取得から5分未満なら何もしない（force=Trueでない場合）
        if not force and (now - self._last_fetch_ts) < FETCH_INTERVAL_SEC:
            return True

        async with self._fetch_lock:
            # ロック取得後に再チェック
            if not force and (time.time() - self._last_fetch_ts) < FETCH_INTERVAL_SEC:
                return True

            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(SCHEDULE_URL)
                    response.raise_for_status()
                    data = response.json()

                schedules_data = data.get("schedules", {})
                # 新形式（モード別）の場合は指定タイプのスケジュールを取得
                if isinstance(schedules_data, dict):
                    self._schedules = schedules_data.get(self._schedule_type, [])
                    available_types = list(schedules_data.keys())
                    if self._schedule_type not in schedules_data:
                        logger.warning(
                            "スケジュールタイプ '{}' が見つかりません。利用可能: {}",
                            self._schedule_type,
                            available_types
                        )
                else:
                    # 旧形式（リスト）の場合はそのまま使用
                    self._schedules = schedules_data
                self._last_fetch_ts = time.time()
                logger.info(
                    "スケジュール取得成功: {}件のスケジュール (タイプ: {})",
                    len(self._schedules),
                    self._schedule_type
                )
                return True

            except httpx.HTTPStatusError as e:
                logger.warning(
                    "スケジュール取得失敗 (HTTP {}): {}",
                    e.response.status_code,
                    e
                )
                return False
            except Exception as e:
                logger.warning("スケジュール取得失敗: {}", e)
                return False

    def get_current_schedule(self) -> Optional[Dict[str, Any]]:
        """現在時刻に該当するスケジュールを取得

        Returns:
            該当するスケジュール辞書、なければNone
        """
        now = datetime.now(timezone.utc)

        for schedule in self._schedules:
            try:
                from_str = schedule.get("from")
                to_str = schedule.get("to")

                if not from_str or not to_str:
                    continue

                # ISO8601形式をパース
                from_dt = datetime.fromisoformat(from_str)
                to_dt = datetime.fromisoformat(to_str)

                # タイムゾーンがない場合はUTCとして扱う
                if from_dt.tzinfo is None:
                    from_dt = from_dt.replace(tzinfo=timezone.utc)
                if to_dt.tzinfo is None:
                    to_dt = to_dt.replace(tzinfo=timezone.utc)

                # 現在時刻が範囲内かチェック
                if from_dt <= now <= to_dt:
                    return schedule

            except Exception as e:
                logger.debug("スケジュールパースエラー: {} - {}", schedule, e)
                continue

        return None

    def is_active(self) -> bool:
        """現在稼働すべきかどうかを判定

        Returns:
            bool: 稼働すべきならTrue
        """
        return self.get_current_schedule() is not None

    def get_lot_coefficient(self) -> float:
        """現在のlot_coefficientを取得

        Returns:
            float: lot_coefficient（スケジュール外なら0.0）
        """
        schedule = self.get_current_schedule()
        if schedule is None:
            return 0.0

        try:
            return float(schedule.get("lot_coefficient", 1.0))
        except (TypeError, ValueError):
            return 1.0

    @property
    def schedules(self) -> List[Dict[str, Any]]:
        """現在保持しているスケジュール一覧"""
        return self._schedules.copy()

    @property
    def last_fetch_time(self) -> Optional[datetime]:
        """最終取得時刻"""
        if self._last_fetch_ts <= 0:
            return None
        return datetime.fromtimestamp(self._last_fetch_ts, tz=timezone.utc)

    @property
    def schedule_type(self) -> str:
        """現在のスケジュールタイプ"""
        return self._schedule_type
