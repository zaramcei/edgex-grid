"""
Grid Trading Engine
グリッド戦略エンジン
"""

import asyncio
import math
import os
from typing import Dict, Optional
import time
from loguru import logger

from bot.adapters.base import ExchangeAdapter
from bot.adapters.edgex_sdk import RateLimitError
from bot.models.types import OrderRequest, OrderSide, OrderType, TimeInForce
from bot.utils.trade_logger import TradeLogger
from bot.schedule_manager import ScheduleManager

class GridEngine:
    """**STEP毎に両サイドへグリッド指値を差し続けなくしたエンジン.
    
    - 剥ぎさない限りキャンセル/差し直しは一切しない
    - 片側levels本オーダー (ENVで指定) を設定価格を中心に両側配置
    - 約定したら価格が戻らない限り再配置しない
    - 価格が動いたら新しい価格帯に不足分の追加（過去の注文は放置）
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        symbol: str,
        poll_interval_sec: float = 1.0,
    ) -> None:
        self.adapter = adapter
        # PydanticやSDKが文字列を要求するため文字列化して保持
        self.symbol = str(symbol)
        self.poll_interval_sec = max(1.5, float(poll_interval_sec))
        self._running = False
        self._loop_iter: int = 0

        self.size = float(os.getenv("EDGEX_GRID_SIZE", os.getenv("EDGEX_SIZE", "0.01")))
        # 既定: ステップ=50USD / 初回オフセット=100USD / レベル=10
        self.step = float(os.getenv("EDGEX_GRID_STEP_USD", "50"))
        # 両側の価格幅(固定) だけ使い込む
        self.first_offset = float(os.getenv("EDGEX_GRID_FIRST_OFFSET_USD", "100"))
        self.levels = int(os.getenv("EDGEX_GRID_LEVELS_PER_SIDE", "10"))
        logger.info(
            "グリッド設定: グリッド幅={}USD 初回オフセット={}USD レベル数={} サイズ={}BTC",
            self.step,
            self.first_offset,
            self.levels,
            self.size,
        )

        # レート制限回避のための遅延時間調整
        try:
            # シンプル高速モードでは既定を短めにする（必要なら環境変数で上書き）
            self.op_spacing_sec = float(os.getenv("EDGEX_GRID_OP_SPACING_SEC", "0.4"))
        except Exception:
            self.op_spacing_sec = 0.4

        # 初回配置済みフラグ（複数回はfirst_offsetは適用しない一度だけ）
        self.initialized = False

        # 既に出した価格（重複防止）
        self.placed_buy_px_to_id: Dict[float, str] = {}
        self.placed_sell_px_to_id: Dict[float, str] = {}

        # ループ内で共有するアクティブ注文キャッシュ（API呼び出し削減用）
        self._cached_active_orders: list = []

        # 定期クリア用タイムスタンプ（1時間に1回）
        self._last_placed_clear_ts: float = time.time()
        self._placed_clear_interval_sec: float = 3600.0  # 1時間

        # 自己クロス回避でスキップされた注文のカウント
        self._self_cross_skip_count: int = 0
        self._last_skip_clear_ts: float = time.time()
        self._skip_clear_interval_sec: float = 3600.0  # 1時間

        self.tlog = TradeLogger()
        # closed PnL poll interval (sec). 0 to disable.
        try:
            self.closed_poll_sec = float(os.getenv("EDGEX_GRID_CLOSED_PNL_SEC", "30"))
        except Exception:
            self.closed_poll_sec = 30.0
        self._last_closed_id: str | None = None
        self._last_closed_poll_ts: float = 0.0

        # 既存の“このBotが出していない注文”を徐々に整理して、levels本に保つ
        try:
            self.enforce_levels = str(os.getenv("EDGEX_GRID_ENFORCE_LEVELS", "1")).lower() in ("1", "true", "yes")
        except Exception:
            self.enforce_levels = True

        # 価格刻み（BOX比較の許容誤差に利用）
        try:
            self.price_tick = float(os.getenv("EDGEX_PRICE_TICK", "0.1"))
        except Exception:
            self.price_tick = 0.1

        # 1ループあたりの新規発注上限（片側）: 明示指定があれば適用（任意）
        try:
            self.max_new_per_loop = int(os.getenv("EDGEX_GRID_MAX_NEW_PER_LOOP", "0"))
        except Exception:
            self.max_new_per_loop = 0

        # シンプルモード（余計な挙動を排し、配置を高速化）
        try:
            self.simple_mode = str(os.getenv("EDGEX_GRID_SIMPLE", "1")).lower() in ("1", "true", "yes")
        except Exception:
            self.simple_mode = True

        # 板を使わずティッカー価格のみで中間価格とみなすモード
        try:
            # 既定: ティッカーのみ（取得が安定）
            self.use_ticker_only = str(os.getenv("EDGEX_USE_TICKER_ONLY", "1")).lower() in ("1", "true", "yes")
        except Exception:
            self.use_ticker_only = True

        # BOX固定モード: 毎ループで P±(X + k*N) の集合に“きっちり”寄せる（余計はキャンセル・欠けは追加）
        try:
            # 既定: BOX（現在価格を中心に毎ループ寄せる）
            self.box_mode = str(os.getenv("EDGEX_GRID_BOX_MODE", "1")).lower() in ("1", "true", "yes")
        except Exception:
            self.box_mode = True

        # 実注文の同期周期（ループ何回に1回か）。BINモードでの整合性確保用
        try:
            self.active_sync_every = int(os.getenv("EDGEX_GRID_ACTIVE_SYNC_EVERY", "3"))
        except Exception:
            self.active_sync_every = 3

        # ビン固定モード: 価格を N 刻みの絶対グリッドに揃える（例: 110000, 110100, 110200 ...）
        # ループ毎に現在価格から目標ビン集合を作り、差分で発注/取消のみ行う
        try:
            # 既定: BINはOFF（強い固定グリッドは任意）
            self.bin_mode = str(os.getenv("EDGEX_GRID_BIN_MODE", "0")).lower() in ("1", "true", "yes")
        except Exception:
            self.bin_mode = False
        # BINモードの現在ビン位置（center_units）を保持し、方向性のインクリメンタル更新に用いる
        self._bin_center_units: int | None = None

        # 価格追従（乖離補正）設定（シンプルモードでは既定OFF）
        try:
            default_follow = "0" if self.simple_mode else "1"
            self.follow_enable = str(os.getenv("EDGEX_GRID_FOLLOW_ENABLE", default_follow)).lower() in ("1", "true", "yes")
        except Exception:
            self.follow_enable = (not self.simple_mode)
        try:
            # X からの許容バンドを N ステップ分だけ広げる（例: 1 -> X+1*N までは許容）
            self.follow_slack_steps = int(os.getenv("EDGEX_GRID_FOLLOW_SLACK_STEPS", "1"))
        except Exception:
            self.follow_slack_steps = 1
        try:
            # 1ループで寄せる最大本数（過度な再配置を抑制）
            self.max_shift_per_loop = int(os.getenv("EDGEX_GRID_MAX_SHIFT_PER_LOOP", "1"))
        except Exception:
            self.max_shift_per_loop = 1

        # スケジュール機能の有効/無効
        use_schedule_env = os.getenv("EDGEX_USE_SCHEDULE", "").lower().strip()
        # 空欄または未設定 → True、"false"/"0"/"no" → False
        if use_schedule_env in ("false", "0", "no"):
            self.use_schedule = False
        else:
            self.use_schedule = True

        # スケジュールマネージャー
        self.schedule_manager = ScheduleManager()
        self._last_schedule_active: bool | None = None  # 前回のアクティブ状態（変化検知用）

        if self.use_schedule:
            logger.info("スケジュール機能: 有効")
        else:
            logger.info("スケジュール機能: 無効 (EDGEX_USE_SCHEDULE=false)")

        # ポジションサイズ制限 (BTC絶対値)
        try:
            # この値以上になったらREDUCE_MODEに移行
            self.position_size_limit = float(os.getenv("EDGEX_POSITION_SIZE_LIMIT_BTC", "0"))
        except Exception:
            self.position_size_limit = 0.0
        try:
            # REDUCE_MODEでこの値を下回るまでポジション積み増し禁止
            self.position_size_reduce_only = float(os.getenv("EDGEX_POSITION_SIZE_REDUCE_ONLY_BTC", "0"))
        except Exception:
            self.position_size_reduce_only = 0.0

        # ポジションサイズ制限 (RATIO: 総資産に対する割合 0.0~1.0)
        # 式: (現在BTC価格 * ポジションサイズ) / 総資産
        try:
            self.position_ratio_limit = float(os.getenv("EDGEX_POSITION_SIZE_LIMIT_RATIO", "0"))
        except Exception:
            self.position_ratio_limit = 0.0
        try:
            self.position_ratio_reduce_only = float(os.getenv("EDGEX_POSITION_SIZE_REDUCE_ONLY_RATIO", "0"))
        except Exception:
            self.position_ratio_reduce_only = 0.0

        # バリデーション: BTCとRATIOは排他的に設定する必要がある
        has_btc = self.position_size_limit > 0 or self.position_size_reduce_only > 0
        has_ratio = self.position_ratio_limit > 0 or self.position_ratio_reduce_only > 0

        if has_btc and has_ratio:
            logger.error("=" * 80)
            logger.error("設定エラー: BTCとRATIOのポジション制限は排他的に設定してください")
            logger.error("  BTC設定: LIMIT={}, REDUCE_ONLY={}", self.position_size_limit, self.position_size_reduce_only)
            logger.error("  RATIO設定: LIMIT={}, REDUCE_ONLY={}", self.position_ratio_limit, self.position_ratio_reduce_only)
            logger.error("どちらか一方のみを設定してください")
            logger.error("=" * 80)
            raise SystemExit(1)

        if not has_btc and not has_ratio:
            logger.error("=" * 80)
            logger.error("設定エラー: ポジションサイズ制限が設定されていません")
            logger.error("以下のいずれかを設定してください:")
            logger.error("  BTC: EDGEX_POSITION_SIZE_LIMIT_BTC / EDGEX_POSITION_SIZE_REDUCE_ONLY_BTC")
            logger.error("  RATIO: EDGEX_POSITION_SIZE_LIMIT_RATIO / EDGEX_POSITION_SIZE_REDUCE_ONLY_RATIO")
            logger.error("=" * 80)
            raise SystemExit(1)

        # 設定されている場合、LIMITとREDUCE_ONLYの両方が必要
        if has_btc:
            if self.position_size_limit <= 0 or self.position_size_reduce_only <= 0:
                logger.error("=" * 80)
                logger.error("設定エラー: BTC制限はLIMITとREDUCE_ONLYの両方を設定する必要があります")
                logger.error("  EDGEX_POSITION_SIZE_LIMIT_BTC={}", self.position_size_limit)
                logger.error("  EDGEX_POSITION_SIZE_REDUCE_ONLY_BTC={}", self.position_size_reduce_only)
                logger.error("=" * 80)
                raise SystemExit(1)
            if self.position_size_reduce_only >= self.position_size_limit:
                logger.error("=" * 80)
                logger.error("設定エラー: REDUCE_ONLY_BTCはLIMIT_BTCより小さい必要があります")
                logger.error("  LIMIT_BTC={} > REDUCE_ONLY_BTC={} であるべき", self.position_size_limit, self.position_size_reduce_only)
                logger.error("=" * 80)
                raise SystemExit(1)
            logger.info(
                "ポジション制限(BTC): LIMIT={:.4f} BTC, REDUCE_ONLY={:.4f} BTC",
                self.position_size_limit, self.position_size_reduce_only
            )

        if has_ratio:
            if self.position_ratio_limit <= 0 or self.position_ratio_reduce_only <= 0:
                logger.error("=" * 80)
                logger.error("設定エラー: RATIO制限はLIMITとREDUCE_ONLYの両方を設定する必要があります")
                logger.error("  EDGEX_POSITION_SIZE_LIMIT_RATIO={}", self.position_ratio_limit)
                logger.error("  EDGEX_POSITION_SIZE_REDUCE_ONLY_RATIO={}", self.position_ratio_reduce_only)
                logger.error("=" * 80)
                raise SystemExit(1)
            if self.position_ratio_reduce_only >= self.position_ratio_limit:
                logger.error("=" * 80)
                logger.error("設定エラー: REDUCE_ONLY_RATIOはLIMIT_RATIOより小さい必要があります")
                logger.error("  LIMIT_RATIO={} > REDUCE_ONLY_RATIO={} であるべき", self.position_ratio_limit, self.position_ratio_reduce_only)
                logger.error("=" * 80)
                raise SystemExit(1)
            logger.info(
                "ポジション制限(RATIO): LIMIT={:.2f}%, REDUCE_ONLY={:.2f}%",
                self.position_ratio_limit, self.position_ratio_reduce_only
            )

        self._reduce_mode = False  # REDUCE_MODEフラグ

    async def _cancel_position_side_orders(self, pos_side: str) -> None:
        """REDUCE_MODE突入時にポジション積み増し方向の既存注文をキャンセルする.

        Args:
            pos_side: ポジションサイド ("LONG" or "SHORT")
        """
        if pos_side == "LONG":
            # LONGポジション → BUY注文をキャンセル
            orders_to_cancel = dict(self.placed_buy_px_to_id)
            side_name = "BUY"
        elif pos_side == "SHORT":
            # SHORTポジション → SELL注文をキャンセル
            orders_to_cancel = dict(self.placed_sell_px_to_id)
            side_name = "SELL"
        else:
            return

        if not orders_to_cancel:
            logger.info("REDUCE_MODE: キャンセル対象の{}注文なし", side_name)
            return

        logger.warning(
            "REDUCE_MODE: ポジション積み増し方向({})の既存注文を全キャンセル 対象={}件",
            side_name, len(orders_to_cancel)
        )

        cancel_count = 0
        for px, order_id in orders_to_cancel.items():
            try:
                await self.adapter.cancel_order(str(order_id))
                cancel_count += 1
                # 内部トラッキングから削除
                if pos_side == "LONG":
                    self.placed_buy_px_to_id.pop(px, None)
                else:
                    self.placed_sell_px_to_id.pop(px, None)
                logger.debug("キャンセル成功: {} price=${:.1f} ID={}", side_name, px, order_id)
                await asyncio.sleep(0.05)  # レート制限対策
            except Exception as e:
                logger.debug("キャンセル失敗: {} price=${:.1f} ID={} error={}", side_name, px, order_id, e)

        logger.warning("REDUCE_MODE: {}注文 {}件をキャンセル完了", side_name, cancel_count)

    def _has_min_gap(self, side_map: Dict[float, str], px: float) -> bool:
        """Return True if `px` is at least `self.step` away from all existing prices in `side_map`."""
        for existing_price in side_map.keys():
            if abs(existing_price - px) < self.step - 1e-9:
                return False
        return True

    async def run(self) -> None:
        await self.adapter.connect()

        # Start WebSocket position monitoring if the adapter supports it
        if hasattr(self.adapter, 'start_position_monitoring'):
            try:
                self.adapter.start_position_monitoring(self.symbol)
                logger.info("WebSocketポジション監視を開始しました")
            except Exception as e:
                logger.warning(f"WebSocketポジション監視の開始に失敗: {e}")

        self._running = True
        logger.info(
            "グリッドエンジン起動: グリッド幅={}USD レベル数={} サイズ={}BTC",
            self.step,
            self.levels,
            self.size,
        )
        logger.debug(
            "grid boot env: step(N)={} offset(X)={} levels={} max_new_per_loop={} enforce_levels={} size={}",
            self.step,
            self.first_offset,
            self.levels,
            self.max_new_per_loop,
            getattr(self, "enforce_levels", True),
            self.size,
        )
        try:
            while self._running:
                try:
                    self._loop_iter += 1
                    logger.debug("グリッドループ開始: iter={} 配置済み買い={}本 配置済み売り={}本 初期化済み={}",
                                self._loop_iter, len(self.placed_buy_px_to_id), len(self.placed_sell_px_to_id), self.initialized)

                    # 定期クリア処理（1時間に1回）
                    self._periodic_clear_placed_maps()

                    # スケジュールチェック（5分に1回取得）
                    if self.use_schedule:
                        await self.schedule_manager.fetch_schedule()
                        is_schedule_active = self.schedule_manager.is_active()

                        # スケジュール状態の変化を検知
                        if self._last_schedule_active is not None and self._last_schedule_active != is_schedule_active:
                            if is_schedule_active:
                                # スケジュール外 → スケジュール内に入った
                                schedule = self.schedule_manager.get_current_schedule()
                                title = schedule.get("title", "Unknown") if schedule else "Unknown"
                                logger.warning("=" * 80)
                                logger.warning("SCHEDULE ACTIVATED: {}", title)
                                logger.warning("=" * 80)
                            else:
                                # スケジュール内 → スケジュール外に出た
                                await self._handle_schedule_exit()

                        self._last_schedule_active = is_schedule_active

                        # スケジュール外なら待機してcontinue
                        if not is_schedule_active:
                            logger.debug("スケジュール外のため待機中...")
                            await asyncio.sleep(self.poll_interval_sec)
                            continue

                    # Check for loss cut trigger
                    has_method = hasattr(self.adapter, 'is_losscut_triggered')
                    is_triggered = self.adapter.is_losscut_triggered() if has_method else False
                    logger.debug(f"Loss cut check: has_method={has_method}, is_triggered={is_triggered}")

                    if has_method and is_triggered:
                        logger.error("=" * 80)
                        logger.error("POSITION LOSS CUT DETECTED - CLOSING ALL POSITIONS")
                        logger.error("=" * 80)

                        try:
                            # STEP 1: Close all positions FIRST (stop loss immediately)
                            logger.warning("STEP 1: Closing all positions immediately...")
                            if hasattr(self.adapter, 'close_position_from_websocket'):
                                closed = await self.adapter.close_position_from_websocket(self.symbol)
                                if closed:
                                    logger.warning("Initial position close order placed")
                                else:
                                    logger.warning("No position to close")
                            else:
                                logger.error("close_position_from_websocket method not available")

                            # STEP 2: Cancel ALL open orders to prevent new positions
                            logger.warning("STEP 2: Canceling all open orders to prevent new positions...")
                            try:
                                active_orders = await self.adapter.list_active_orders(self.symbol)
                                cancel_count = 0
                                for order in active_orders:
                                    try:
                                        order_id = (
                                            order.get("orderId")
                                            or order.get("id")
                                            or order.get("order_id")
                                            or order.get("clientOrderId")
                                        )
                                        if order_id:
                                            await self.adapter.cancel_order(str(order_id))
                                            cancel_count += 1
                                            await asyncio.sleep(0.1)
                                    except Exception as e:
                                        logger.debug(f"Failed to cancel order {order_id}: {e}")
                                logger.warning(f"Canceled {cancel_count} open orders")

                                # Clear our internal tracking
                                self.placed_buy_px_to_id.clear()
                                self.placed_sell_px_to_id.clear()
                                self._cached_active_orders = []
                            except Exception as e:
                                logger.error(f"Error canceling orders: {e}", exc_info=True)

                            # STEP 3: Re-check and close any remaining positions (in case orders filled during close)
                            logger.warning("STEP 3: Re-checking for any remaining positions...")
                            await asyncio.sleep(2.0)  # Wait for orders to settle
                            if hasattr(self.adapter, 'close_position_from_websocket'):
                                closed_again = await self.adapter.close_position_from_websocket(self.symbol)
                                if closed_again:
                                    logger.warning("Closed remaining positions that opened during initial close")
                                else:
                                    logger.info("No remaining positions found - all clear")

                            # STEP 4: Reset the loss cut flag on the WebSocket client
                            if (hasattr(self.adapter, '_ws_client_private') and
                                self.adapter._ws_client_private and
                                hasattr(self.adapter._ws_client_private, 'losscut_triggered')):
                                self.adapter._ws_client_private.losscut_triggered = False

                            logger.warning("=" * 80)
                            logger.warning("POSITION LOSS CUT - All positions closed, pausing for cooldown")
                            logger.warning("=" * 80)

                            # STEP 5: Wait for cooldown period to ensure everything settles
                            cooldown_sec = 30.0
                            logger.warning(f"Waiting {cooldown_sec} seconds for positions to settle...")
                            await asyncio.sleep(cooldown_sec)

                            logger.warning("Cooldown complete, resuming grid trading")
                            continue

                        except Exception as e:
                            logger.error(f"Error during loss cut reset: {e}", exc_info=True)
                            # Continue anyway to avoid stopping the bot
                            await asyncio.sleep(self.poll_interval_sec)
                            continue

                    # Check for position-based take profit trigger
                    if hasattr(self.adapter, 'is_takeprofit_triggered') and self.adapter.is_takeprofit_triggered():
                        logger.warning("=" * 80)
                        logger.warning("POSITION TAKE PROFIT DETECTED - CLOSING ALL POSITIONS")
                        logger.warning("=" * 80)

                        try:
                            # STEP 1: Close all positions FIRST (lock in profit immediately)
                            logger.warning("STEP 1: Closing all positions immediately...")
                            if hasattr(self.adapter, 'close_position_from_websocket'):
                                closed = await self.adapter.close_position_from_websocket(self.symbol)
                                if closed:
                                    logger.warning("Initial position close order placed")
                                else:
                                    logger.warning("No position to close")
                            else:
                                logger.error("close_position_from_websocket method not available")

                            # STEP 2: Cancel ALL open orders to prevent new positions
                            logger.warning("STEP 2: Canceling all open orders to prevent new positions...")
                            try:
                                active_orders = await self.adapter.list_active_orders(self.symbol)
                                cancel_count = 0
                                for order in active_orders:
                                    try:
                                        order_id = (
                                            order.get("orderId")
                                            or order.get("id")
                                            or order.get("order_id")
                                            or order.get("clientOrderId")
                                        )
                                        if order_id:
                                            await self.adapter.cancel_order(str(order_id))
                                            cancel_count += 1
                                            await asyncio.sleep(0.1)
                                    except Exception as e:
                                        logger.debug(f"Failed to cancel order {order_id}: {e}")
                                logger.warning(f"Canceled {cancel_count} open orders")

                                # Clear our internal tracking
                                self.placed_buy_px_to_id.clear()
                                self.placed_sell_px_to_id.clear()
                                self._cached_active_orders = []
                            except Exception as e:
                                logger.error(f"Error canceling orders: {e}", exc_info=True)

                            # STEP 3: Re-check and close any remaining positions (in case orders filled during close)
                            logger.warning("STEP 3: Re-checking for any remaining positions...")
                            await asyncio.sleep(2.0)  # Wait for orders to settle
                            if hasattr(self.adapter, 'close_position_from_websocket'):
                                closed_again = await self.adapter.close_position_from_websocket(self.symbol)
                                if closed_again:
                                    logger.warning("Closed remaining positions that opened during initial close")
                                else:
                                    logger.info("No remaining positions found - all clear")

                            # STEP 4: Reset the take profit flag on the WebSocket client (reuses losscut_triggered flag)
                            if (hasattr(self.adapter, '_ws_client_private') and
                                self.adapter._ws_client_private and
                                hasattr(self.adapter._ws_client_private, 'losscut_triggered')):
                                self.adapter._ws_client_private.losscut_triggered = False

                            logger.warning("=" * 80)
                            logger.warning("POSITION TAKE PROFIT - All positions closed, pausing for cooldown")
                            logger.warning("=" * 80)

                            # STEP 5: Wait for cooldown period to ensure everything settles
                            cooldown_sec = 30.0
                            logger.warning(f"Waiting {cooldown_sec} seconds for positions to settle...")
                            await asyncio.sleep(cooldown_sec)

                            logger.warning("Cooldown complete, resuming grid trading")
                            continue

                        except Exception as e:
                            logger.error(f"Error during position take profit: {e}", exc_info=True)
                            # Continue anyway to avoid stopping the bot
                            await asyncio.sleep(self.poll_interval_sec)
                            continue

                    # Check for balance recovery trigger
                    if hasattr(self.adapter, 'is_balance_recovery_triggered') and self.adapter.is_balance_recovery_triggered():
                        logger.warning("=" * 80)
                        logger.warning("BALANCE RECOVERY DETECTED - CLOSING ALL POSITIONS")
                        logger.warning("=" * 80)

                        try:
                            # STEP 1: Close all positions FIRST (lock in recovery immediately)
                            logger.warning("STEP 1: Closing all positions immediately...")
                            if hasattr(self.adapter, 'close_position_from_websocket'):
                                closed = await self.adapter.close_position_from_websocket(self.symbol)
                                if closed:
                                    logger.warning("Initial position close order placed")
                                else:
                                    logger.warning("No position to close")
                            else:
                                logger.error("close_position_from_websocket method not available")

                            # STEP 2: Cancel ALL open orders to prevent new positions
                            logger.warning("STEP 2: Canceling all open orders to prevent new positions...")
                            try:
                                active_orders = await self.adapter.list_active_orders(self.symbol)
                                cancel_count = 0
                                for order in active_orders:
                                    try:
                                        order_id = (
                                            order.get("orderId")
                                            or order.get("id")
                                            or order.get("order_id")
                                            or order.get("clientOrderId")
                                        )
                                        if order_id:
                                            await self.adapter.cancel_order(str(order_id))
                                            cancel_count += 1
                                            await asyncio.sleep(0.1)
                                    except Exception as e:
                                        logger.debug(f"Failed to cancel order {order_id}: {e}")
                                logger.warning(f"Canceled {cancel_count} open orders")

                                # Clear our internal tracking
                                self.placed_buy_px_to_id.clear()
                                self.placed_sell_px_to_id.clear()
                                self._cached_active_orders = []
                            except Exception as e:
                                logger.error(f"Error canceling orders: {e}", exc_info=True)

                            # STEP 3: Re-check and close any remaining positions (in case orders filled during close)
                            logger.warning("STEP 3: Re-checking for any remaining positions...")
                            await asyncio.sleep(2.0)  # Wait for orders to settle
                            if hasattr(self.adapter, 'close_position_from_websocket'):
                                closed_again = await self.adapter.close_position_from_websocket(self.symbol)
                                if closed_again:
                                    logger.warning("Closed remaining positions that opened during initial close")
                                else:
                                    logger.info("No remaining positions found - all clear")

                            # STEP 4: Reset the balance recovery flag on the WebSocket client
                            if (hasattr(self.adapter, '_ws_client_private') and
                                self.adapter._ws_client_private and
                                hasattr(self.adapter._ws_client_private, 'balance_recovery_triggered')):
                                self.adapter._ws_client_private.balance_recovery_triggered = False

                            logger.warning("=" * 80)
                            logger.warning("BALANCE RECOVERY - All positions closed, pausing for cooldown")
                            logger.warning("=" * 80)

                            # STEP 5: Wait for cooldown period to ensure everything settles
                            cooldown_sec = 30.0
                            logger.warning(f"Waiting {cooldown_sec} seconds for positions to settle...")
                            await asyncio.sleep(cooldown_sec)

                            logger.warning("Cooldown complete, resuming grid trading")
                            continue

                        except Exception as e:
                            logger.error(f"Error during balance recovery: {e}", exc_info=True)
                            # Continue anyway to avoid stopping the bot
                            await asyncio.sleep(self.poll_interval_sec)
                            continue

                    # Check for asset-based loss cut trigger
                    has_losscut_method = hasattr(self.adapter, 'is_asset_losscut_triggered')
                    is_losscut_triggered = self.adapter.is_asset_losscut_triggered() if has_losscut_method else False
                    logger.info(f"Asset loss cut check: has_method={has_losscut_method}, is_triggered={is_losscut_triggered}")

                    if has_losscut_method and is_losscut_triggered:
                        logger.error("=" * 80)
                        logger.error("ASSET-BASED LOSS CUT DETECTED - CLOSING ALL POSITIONS")
                        logger.error("=" * 80)

                        try:
                            # STEP 1: Close all positions FIRST (stop loss immediately)
                            logger.warning("STEP 1: Closing all positions immediately...")
                            if hasattr(self.adapter, 'close_position_from_websocket'):
                                closed = await self.adapter.close_position_from_websocket(self.symbol)
                                if closed:
                                    logger.warning("Initial position close order placed")
                                else:
                                    logger.warning("No position to close")
                            else:
                                logger.error("close_position_from_websocket method not available")

                            # STEP 2: Cancel ALL open orders to prevent new positions
                            logger.warning("STEP 2: Canceling all open orders to prevent new positions...")
                            try:
                                active_orders = await self.adapter.list_active_orders(self.symbol)
                                cancel_count = 0
                                for order in active_orders:
                                    try:
                                        order_id = (
                                            order.get("orderId")
                                            or order.get("id")
                                            or order.get("order_id")
                                            or order.get("clientOrderId")
                                        )
                                        if order_id:
                                            await self.adapter.cancel_order(str(order_id))
                                            cancel_count += 1
                                            await asyncio.sleep(0.1)
                                    except Exception as e:
                                        logger.debug(f"Failed to cancel order {order_id}: {e}")
                                logger.warning(f"Canceled {cancel_count} open orders")

                                # Clear our internal tracking
                                self.placed_buy_px_to_id.clear()
                                self.placed_sell_px_to_id.clear()
                                self._cached_active_orders = []
                            except Exception as e:
                                logger.error(f"Error canceling orders: {e}", exc_info=True)

                            # STEP 3: Re-check and close any remaining positions (in case orders filled during close)
                            logger.warning("STEP 3: Re-checking for any remaining positions...")
                            await asyncio.sleep(2.0)  # Wait for orders to settle
                            if hasattr(self.adapter, 'close_position_from_websocket'):
                                closed_again = await self.adapter.close_position_from_websocket(self.symbol)
                                if closed_again:
                                    logger.warning("Closed remaining positions that opened during initial close")
                                else:
                                    logger.info("No remaining positions found - all clear")

                            # STEP 4: Reset the asset loss cut flag and update initial asset
                            if (hasattr(self.adapter, '_ws_client_private') and
                                self.adapter._ws_client_private and
                                hasattr(self.adapter._ws_client_private, 'asset_losscut_triggered')):
                                self.adapter._ws_client_private.asset_losscut_triggered = False

                                # Update initial asset to current asset to reset the baseline
                                if hasattr(self.adapter._ws_client_private, 'current_balance'):
                                    current_balance = self.adapter._ws_client_private.current_balance
                                    if current_balance is not None:
                                        self.adapter._ws_client_private.initial_asset = current_balance
                                        logger.warning(f"Reset initial asset to current balance: {current_balance:.2f} USD")

                            logger.warning("=" * 80)
                            logger.warning("ASSET-BASED LOSS CUT - All positions closed, pausing for cooldown")
                            logger.warning("=" * 80)

                            # STEP 5: Wait for cooldown period to ensure everything settles
                            cooldown_sec = 30.0
                            logger.warning(f"Waiting {cooldown_sec} seconds for positions to settle...")
                            await asyncio.sleep(cooldown_sec)

                            logger.warning("Cooldown complete, resuming grid trading")
                            continue

                        except Exception as e:
                            logger.error(f"Error during asset-based loss cut: {e}", exc_info=True)
                            # Continue anyway to avoid stopping the bot
                            await asyncio.sleep(self.poll_interval_sec)
                            continue

                    # Check for asset-based take profit trigger
                    if hasattr(self.adapter, 'is_asset_takeprofit_triggered') and self.adapter.is_asset_takeprofit_triggered():
                        logger.warning("=" * 80)
                        logger.warning("ASSET-BASED TAKE PROFIT DETECTED - CLOSING ALL POSITIONS")
                        logger.warning("=" * 80)

                        try:
                            # STEP 1: Close all positions FIRST (lock in profit immediately)
                            logger.warning("STEP 1: Closing all positions immediately...")
                            if hasattr(self.adapter, 'close_position_from_websocket'):
                                closed = await self.adapter.close_position_from_websocket(self.symbol)
                                if closed:
                                    logger.warning("Initial position close order placed")
                                else:
                                    logger.warning("No position to close")
                            else:
                                logger.error("close_position_from_websocket method not available")

                            # STEP 2: Cancel ALL open orders to prevent new positions
                            logger.warning("STEP 2: Canceling all open orders to prevent new positions...")
                            try:
                                active_orders = await self.adapter.list_active_orders(self.symbol)
                                cancel_count = 0
                                for order in active_orders:
                                    try:
                                        order_id = (
                                            order.get("orderId")
                                            or order.get("id")
                                            or order.get("order_id")
                                            or order.get("clientOrderId")
                                        )
                                        if order_id:
                                            await self.adapter.cancel_order(str(order_id))
                                            cancel_count += 1
                                            await asyncio.sleep(0.1)
                                    except Exception as e:
                                        logger.debug(f"Failed to cancel order {order_id}: {e}")
                                logger.warning(f"Canceled {cancel_count} open orders")

                                # Clear our internal tracking
                                self.placed_buy_px_to_id.clear()
                                self.placed_sell_px_to_id.clear()
                                self._cached_active_orders = []
                            except Exception as e:
                                logger.error(f"Error canceling orders: {e}", exc_info=True)

                            # STEP 3: Re-check and close any remaining positions (in case orders filled during close)
                            logger.warning("STEP 3: Re-checking for any remaining positions...")
                            await asyncio.sleep(2.0)  # Wait for orders to settle
                            if hasattr(self.adapter, 'close_position_from_websocket'):
                                closed_again = await self.adapter.close_position_from_websocket(self.symbol)
                                if closed_again:
                                    logger.warning("Closed remaining positions that opened during initial close")
                                else:
                                    logger.info("No remaining positions found - all clear")

                            # STEP 4: Reset the asset take profit flag and update initial asset
                            if (hasattr(self.adapter, '_ws_client_private') and
                                self.adapter._ws_client_private and
                                hasattr(self.adapter._ws_client_private, 'asset_takeprofit_triggered')):
                                self.adapter._ws_client_private.asset_takeprofit_triggered = False

                                # Update initial asset to current asset to reset the baseline
                                if hasattr(self.adapter._ws_client_private, 'current_balance'):
                                    current_balance = self.adapter._ws_client_private.current_balance
                                    if current_balance is not None:
                                        self.adapter._ws_client_private.initial_asset = current_balance
                                        logger.warning(f"Reset initial asset to current balance: {current_balance:.2f} USD")

                            logger.warning("=" * 80)
                            logger.warning("ASSET-BASED TAKE PROFIT - All positions closed, pausing for cooldown")
                            logger.warning("=" * 80)

                            # STEP 5: Wait for cooldown period to ensure everything settles
                            cooldown_sec = 30.0
                            logger.warning(f"Waiting {cooldown_sec} seconds for positions to settle...")
                            await asyncio.sleep(cooldown_sec)

                            logger.warning("Cooldown complete, resuming grid trading")
                            continue

                        except Exception as e:
                            logger.error(f"Error during asset-based take profit: {e}", exc_info=True)
                            # Continue anyway to avoid stopping the bot
                            await asyncio.sleep(self.poll_interval_sec)
                            continue

                    # 現在価格取得 - WebSocketから取得（レート制限回避）
                    try:
                        # まずWebSocketから価格を取得
                        mid_price = None
                        if hasattr(self.adapter, 'get_current_price_from_websocket'):
                            ws_price = self.adapter.get_current_price_from_websocket()
                            if ws_price is not None:
                                mid_price = ws_price
                                logger.debug(f"Using WebSocket price: {mid_price}")

                        # WebSocket価格が取得できない場合のみREST APIにフォールバック
                        if mid_price is None:
                            if getattr(self, "use_ticker_only", False):
                                ticker = await self.adapter.get_ticker(self.symbol)
                                mid_price = float(ticker.price)
                            else:
                                # まず板の最良気配からミッド算出。無ければティッカー。
                                bid, ask = await self.adapter.get_best_bid_ask(self.symbol)
                                if bid is not None and ask is not None:
                                    mid_price = (float(bid) + float(ask)) / 2.0
                                else:
                                    ticker = await self.adapter.get_ticker(self.symbol)
                                    mid_price = float(ticker.price)
                            logger.debug(f"Using REST API price: {mid_price}")
                    except Exception as e:
                        logger.warning("価格取得に失敗: {}", e)
                        await asyncio.sleep(self.poll_interval_sec)
                        continue

                    logger.debug(
                        "loop ctx: P={} X={} N={} levels={} placed_buy={} placed_sell={}",
                        mid_price,
                        self.first_offset,
                        self.step,
                        self.levels,
                        sorted(self.placed_buy_px_to_id.keys()),
                        sorted(self.placed_sell_px_to_id.keys()),
                    )

                    # 毎ループ: ポジションを取得して軽く表示（ズレの観測用）
                    try:
                        positions = await getattr(self.adapter, "fetch_positions", lambda *_args, **_kw: [])(self.symbol)
                        net_size = 0.0
                        for p in (positions or []):
                            try:
                                sz_raw = p.get("size") or p.get("positionSize") or p.get("qty")
                                if sz_raw is None:
                                    continue
                                sz = float(sz_raw)
                                side = str(p.get("side") or p.get("positionSide") or "").upper()
                                if side in ("SHORT", "SELL"):
                                    sz = -abs(sz)
                                elif side in ("LONG", "BUY"):
                                    sz = abs(sz)
                                # 一部APIは符号付きサイズのみを返すためそのまま加算
                                net_size += sz
                            except Exception:
                                continue
                        logger.debug("pos: net_size={} raw_count={}", net_size, len(positions or []))
                    except Exception:
                        pass

                    # ループ頭で1回だけlist_active_ordersを取得しキャッシュ（429対策）
                    try:
                        self._cached_active_orders = await self.adapter.list_active_orders(self.symbol)
                    except Exception as e:
                        logger.debug("list_active_orders failed (use stale cache): {}", e)
                        # 失敗した場合は既存キャッシュを使い続ける

                    # 周期的に取引所のOPEN注文と突合（3ループに1回など）
                    if getattr(self, "active_sync_every", 0) > 0 and (self._loop_iter % self.active_sync_every == 0):
                        self._sync_active_orders_from_cache()

                    # グリッド配置
                    await self._ensure_grid(mid_price)

                    # 約定確認と補充
                    await self._replenish_if_filled()

                except RateLimitError as e:
                    # 429レートリミット検出時はループをスキップして待機
                    logger.warning("429レートリミット検出、ループをスキップ: {}", e)
                    await asyncio.sleep(self.poll_interval_sec)
                    continue

                except Exception as e:
                    logger.warning("グリッドループエラー: {}", e)
                    logger.debug("グリッドループ終了: iter={} 待機時間={}秒", self._loop_iter, self.poll_interval_sec)
                    await asyncio.sleep(self.poll_interval_sec)
                    continue

                # 定期: クローズ損益の新規行を取り込み
                await self._poll_closed_pnl_once()

                # 正常時も必ず待機してAPI連打を抑制（429対策）
                logger.debug("グリッドループ終了: iter={} 待機時間={}秒", self._loop_iter, self.poll_interval_sec)
                await asyncio.sleep(self.poll_interval_sec)

        finally:
            await self.adapter.close()
            logger.info("グリッドエンジン停止")

    def _sync_active_orders_from_cache(self) -> None:
        """キャッシュされたOPEN注文から内部マップを同期する（API呼び出しなし）。"""
        active_orders = self._cached_active_orders

        new_buys: Dict[float, str] = {}
        new_sells: Dict[float, str] = {}

        def _px(row: dict) -> float | None:
            try:
                raw = row.get("price") or row.get("px") or row.get("0")
                return float(raw) if raw is not None else None
            except Exception:
                return None

        def _oid(row: dict) -> str | None:
            oid = (
                row.get("orderId")
                or row.get("id")
                or row.get("order_id")
                or row.get("clientOrderId")
                or row.get("client_order_id")
            )
            return str(oid) if oid else None

        for row in (active_orders or []):
            if not isinstance(row, dict):
                continue
            # 状態/シンボル/サイド/価格
            status = str(row.get("status") or "").upper()
            if status and status != "OPEN":
                continue
            # 価格
            px = _px(row)
            if px is None:
                continue
            # サイド
            side_str = str(row.get("side") or row.get("orderSide") or "").upper()
            oid = _oid(row)
            if not oid or not side_str:
                continue
            if side_str in ("BUY", "LONG"):
                new_buys[px] = oid
            elif side_str in ("SELL", "SHORT"):
                new_sells[px] = oid

        self.placed_buy_px_to_id = new_buys
        self.placed_sell_px_to_id = new_sells
        logger.debug("active sync: buy={} sell={}", len(new_buys), len(new_sells))

    def _remove_from_cache(self, order_id: str) -> None:
        """キャッシュから指定IDの注文を削除する（キャンセル成功時に呼ぶ）"""
        self._cached_active_orders = [
            o for o in self._cached_active_orders
            if str(o.get("orderId") or o.get("id") or o.get("order_id") or "") != order_id
        ]

    def _add_to_cache(self, order_id: str, side: str, price: float) -> None:
        """キャッシュに注文を追加する（発注成功時に呼ぶ）"""
        self._cached_active_orders.append({
            "orderId": order_id,
            "side": side,
            "price": str(price),
            "status": "OPEN",
        })

    async def _ensure_grid(self, mid_price: float):
        """
        現在価格Pから内側Xを空け、P±(X + k*N) の等差列だけに指値を配置。
        - 買い: P - (X + k*N)
        - 売り: P + (X + k*N)
        既に置いてある価格はスキップ。自サイドの最も近い注文とN未満にならないようにする。
        """
        if self.step <= 0:
            return

        # === BOXモード: 価格周りのボックスを毎ループ厳密維持（寄せる） ===
        if getattr(self, "box_mode", False):
            # 固定ラティス: 価格は step の絶対グリッド（…0, step, 2*step, ...）に揃える。
            # 現在価格 P は「内側禁止帯 X」と本数選定だけに利用し、位置決めには使わない。
            P = float(mid_price)
            s = float(self.step)
            X = float(self.first_offset)

            # 買い側: P-X より下で最も近いグリッドから levels 本
            lower_limit = P - X - 1e-9
            buy_start = math.floor(lower_limit / s) * s
            buy_targets = [buy_start - i * s for i in range(self.levels)]

            # 売り側: P+X より上で最も近いグリッドから levels 本
            upper_limit = P + X + 1e-9
            sell_start = math.ceil(upper_limit / s) * s
            sell_targets = [sell_start + i * s for i in range(self.levels)]

            # 作業用に丸め（浮動小数の微小誤差対策）
            def _r(x: float) -> float:
                return round(float(x), 10)

            buy_targets = [_r(px) for px in buy_targets if px > 0 and px < (P - 1e-9)]
            sell_targets = [_r(px) for px in sell_targets if px > (P + 1e-9)]

            current_buys = set(_r(px) for px in self.placed_buy_px_to_id.keys())
            current_sells = set(_r(px) for px in self.placed_sell_px_to_id.keys())
            target_buys = set(buy_targets)
            target_sells = set(sell_targets)

            # 許容誤差内なら“同一ターゲット扱い”にする（clamp等で微妙にズレても維持）
            tol = max(self.price_tick * 1.01, 1e-6)
            def _near_any(x: float, targets: set[float]) -> bool:
                for t in targets:
                    if abs(x - t) <= tol:
                        return True
                return False

            keep_buys = set(px for px in current_buys if _near_any(px, target_buys))
            keep_sells = set(px for px in current_sells if _near_any(px, target_sells))

            # 内側の既存注文は必ず維持（取り消さない）
            inner_buy_border = P - X
            inner_sell_border = P + X
            keep_buys |= set(px for px in current_buys if px >= (inner_buy_border - tol))
            keep_sells |= set(px for px in current_sells if px <= (inner_sell_border + tol))

            # 余計（ターゲット外で近似も無し）だけキャンセル
            for px in sorted(current_buys - keep_buys):
                try:
                    oid = self.placed_buy_px_to_id.pop(px)
                except KeyError:
                    continue
                try:
                    await self.adapter.cancel_order(oid)
                except Exception:
                    pass
                await asyncio.sleep(self.op_spacing_sec)

            for px in sorted(current_sells - keep_sells):
                try:
                    oid = self.placed_sell_px_to_id.pop(px)
                except KeyError:
                    continue
                try:
                    await self.adapter.cancel_order(oid)
                except Exception:
                    pass
                await asyncio.sleep(self.op_spacing_sec)

            # 欠け（近似含め存在しないターゲット）を追加（交互発注・ポジションクローズ方向優先・価格近い順）
            # BUYは現在価格に近い順（降順）、SELLは現在価格に近い順（昇順）
            missing_buys = sorted([px for px in target_buys if not any(abs(cb - px) <= tol for cb in keep_buys)], reverse=True)
            missing_sells = sorted([px for px in target_sells if not any(abs(cs - px) <= tol for cs in keep_sells)])

            if missing_buys or missing_sells:
                # ポジション方向を取得してクローズ方向を優先
                _, pos_side = self._get_current_position_size_and_side()
                close_first = "SELL" if pos_side != "SHORT" else "BUY"

                buy_iter = iter(missing_buys)
                sell_iter = iter(missing_sells)
                current_side = close_first

                while True:
                    placed = False
                    if current_side == "BUY":
                        px = next(buy_iter, None)
                        if px is not None and self._has_min_gap(self.placed_buy_px_to_id, px):
                            await self._place_order(OrderSide.BUY, px)
                            await asyncio.sleep(self.op_spacing_sec)
                            placed = True
                    else:
                        px = next(sell_iter, None)
                        if px is not None and self._has_min_gap(self.placed_sell_px_to_id, px):
                            await self._place_order(OrderSide.SELL, px)
                            await asyncio.sleep(self.op_spacing_sec)
                            placed = True

                    current_side = "SELL" if current_side == "BUY" else "BUY"

                    # 両方のイテレータが尽きたら終了
                    if not placed:
                        # もう一方も試す
                        if current_side == "BUY":
                            px = next(buy_iter, None)
                            if px is not None and self._has_min_gap(self.placed_buy_px_to_id, px):
                                await self._place_order(OrderSide.BUY, px)
                                await asyncio.sleep(self.op_spacing_sec)
                                continue
                        else:
                            px = next(sell_iter, None)
                            if px is not None and self._has_min_gap(self.placed_sell_px_to_id, px):
                                await self._place_order(OrderSide.SELL, px)
                                await asyncio.sleep(self.op_spacing_sec)
                                continue
                        break

            # 初期化フラグ
            if not self.initialized:
                self.initialized = True
                logger.info("BOX: 初期配置完了 買い{}本 売り{}本", len(self.placed_buy_px_to_id), len(self.placed_sell_px_to_id))
            return

        # === BIN固定モード: 方向性インクリメンタル ===
        if self.bin_mode:
            # 中心を「stepの整数倍」に丸める（例: step=100, P=100,050 → center=100,100）
            try:
                center_units = round(float(mid_price) / self.step)
                center = float(center_units * self.step)
            except Exception:
                center = float(mid_price)
                center_units = round(center / self.step)
            # 初回: 目標列を構築して配置（交互発注・ポジションクローズ方向優先・価格近い順）
            if not self.initialized:
                # 現在価格に近い順にソート（BUYは降順=高い方から、SELLは昇順=低い方から）
                buy_targets = sorted([center - k * self.step for k in range(self.levels, 0, -1)], reverse=True)
                sell_targets = sorted([center + k * self.step for k in range(1, self.levels + 1)])

                # ポジション方向を取得してクローズ方向を優先
                _, pos_side = self._get_current_position_size_and_side()
                # LONG → 先にSELL（クローズ方向）, SHORT → 先にBUY, なし → SELL優先
                close_first = "SELL" if pos_side != "SHORT" else "BUY"
                logger.info("BIN: 初期配置開始 pos_side={} close_first={}", pos_side, close_first)

                # 交互発注用のイテレータ
                buy_iter = iter(buy_targets)
                sell_iter = iter(sell_targets)
                add_buys = 0
                add_sells = 0
                total_max = self.max_new_per_loop * 2 if self.max_new_per_loop else None

                # 交互に発注（クローズ方向から開始）
                current_side = close_first
                while True:
                    if total_max and (add_buys + add_sells) >= total_max:
                        break

                    placed = False
                    if current_side == "BUY" and add_buys < self.levels:
                        px = next(buy_iter, None)
                        if px is not None:
                            await self._place_order(OrderSide.BUY, px)
                            add_buys += 1
                            await asyncio.sleep(self.op_spacing_sec)
                            placed = True
                    elif current_side == "SELL" and add_sells < self.levels:
                        px = next(sell_iter, None)
                        if px is not None:
                            await self._place_order(OrderSide.SELL, px)
                            add_sells += 1
                            await asyncio.sleep(self.op_spacing_sec)
                            placed = True

                    # サイドを交互に切り替え
                    current_side = "SELL" if current_side == "BUY" else "BUY"

                    # 両方のイテレータが尽きたら終了
                    if add_buys >= self.levels and add_sells >= self.levels:
                        break
                    # 片方が尽きてもう片方も発注できなかった場合
                    if not placed and add_buys >= self.levels and add_sells >= self.levels:
                        break
                    # 無限ループ防止
                    if add_buys >= self.levels and add_sells >= self.levels:
                        break

                self.initialized = True
                self._bin_center_units = center_units
                logger.info("BIN: 初期配置完了 買い{}本 売り{}本", len(self.placed_buy_px_to_id), len(self.placed_sell_px_to_id))
                return

            # 以降: 方向性インクリメンタル（近い側は触らない）
            prev_units = self._bin_center_units if self._bin_center_units is not None else center_units
            delta_units = center_units - prev_units

            # 変化なし → レベル不足なら現在センター基準で再シード/補充（近い側は既存を優先し、欠けている価格のみ追加）
            if delta_units == 0:
                try:
                    buy_targets = [center - k * self.step for k in range(self.levels, 0, -1)]
                    sell_targets = [center + k * self.step for k in range(1, self.levels + 1)]

                    # BUY不足: ターゲット列から欠けている価格を追加（キャンセルはしない）
                    if len(self.placed_buy_px_to_id) < self.levels:
                        for px in buy_targets:
                            if len(self.placed_buy_px_to_id) >= self.levels:
                                break
                            if px not in self.placed_buy_px_to_id:
                                await self._place_order(OrderSide.BUY, px)
                                await asyncio.sleep(self.op_spacing_sec)

                    # SELL不足: ターゲット列から欠けている価格を追加（キャンセルはしない）
                    if len(self.placed_sell_px_to_id) < self.levels:
                        for px in sell_targets:
                            if len(self.placed_sell_px_to_id) >= self.levels:
                                break
                            if px not in self.placed_sell_px_to_id:
                                await self._place_order(OrderSide.SELL, px)
                                await asyncio.sleep(self.op_spacing_sec)
                except Exception as e:
                    logger.debug("BIN: 補充スキップ {}", e)
                return

            steps = int(abs(delta_units))
            direction_up = delta_units > 0

            for _ in range(steps):
                if direction_up:
                    # 上昇: BUYのみ内側へ1段スライド（遠いBUYを消して近い側へ+Nで追加）
                    if self.placed_buy_px_to_id:
                        far_buy_px = min(self.placed_buy_px_to_id.keys())
                        far_buy_id = self.placed_buy_px_to_id.pop(far_buy_px)
                        try:
                            await self.adapter.cancel_order(far_buy_id)
                        except Exception:
                            logger.debug("BIN↑: 遠いBUYキャンセル失敗(無視) id={} px={}", far_buy_id, far_buy_px)
                        await asyncio.sleep(self.op_spacing_sec)

                        near_buy = max(self.placed_buy_px_to_id.keys()) if self.placed_buy_px_to_id else (center - self.step)
                        new_near_buy = near_buy + self.step
                        if new_near_buy < (mid_price - 1e-9) and new_near_buy not in self.placed_buy_px_to_id and self._has_min_gap(self.placed_buy_px_to_id, new_near_buy):
                            await self._place_order(OrderSide.BUY, new_near_buy)
                            await asyncio.sleep(self.op_spacing_sec)

                    # SELLはキャンセルせず、最外のさらに外側に1本だけ追加
                    if self.placed_sell_px_to_id:
                        far_sell_px = max(self.placed_sell_px_to_id.keys())
                        new_outer_sell = far_sell_px + self.step
                        if new_outer_sell > (mid_price + 1e-9) \
                            and new_outer_sell not in self.placed_sell_px_to_id \
                            and self._has_min_gap(self.placed_sell_px_to_id, new_outer_sell):
                            await self._place_order(OrderSide.SELL, new_outer_sell)
                            await asyncio.sleep(self.op_spacing_sec)
                else:
                    # 下降: SELLのみ内側へ1段スライド
                    if self.placed_sell_px_to_id:
                        far_sell_px = max(self.placed_sell_px_to_id.keys())
                        far_sell_id = self.placed_sell_px_to_id.pop(far_sell_px)
                        try:
                            await self.adapter.cancel_order(far_sell_id)
                        except Exception:
                            logger.debug("BIN↓: 遠いSELLキャンセル失敗(無視) id={} px={}", far_sell_id, far_sell_px)
                        await asyncio.sleep(self.op_spacing_sec)

                        near_sell = min(self.placed_sell_px_to_id.keys()) if self.placed_sell_px_to_id else (center + self.step)
                        new_near_sell = near_sell - self.step
                        if new_near_sell > (mid_price + 1e-9) and new_near_sell not in self.placed_sell_px_to_id and self._has_min_gap(self.placed_sell_px_to_id, new_near_sell):
                            await self._place_order(OrderSide.SELL, new_near_sell)
                            await asyncio.sleep(self.op_spacing_sec)

                    # BUYはキャンセルせず、最外のさらに外側に1本だけ追加
                    if self.placed_buy_px_to_id:
                        far_buy_px = min(self.placed_buy_px_to_id.keys())
                        new_outer_buy = far_buy_px - self.step
                        if new_outer_buy > 0 \
                            and new_outer_buy < (mid_price - 1e-9) \
                            and new_outer_buy not in self.placed_buy_px_to_id \
                            and self._has_min_gap(self.placed_buy_px_to_id, new_outer_buy):
                            await self._place_order(OrderSide.BUY, new_outer_buy)
                            await asyncio.sleep(self.op_spacing_sec)

            self._bin_center_units = center_units
            return

        # 初期配置後:
        # - 片側が全滅していたら、その片側だけ現在価格Pから再配置（挟み込みを回復）
        # - 両側に1本以上あれば、ここでは新規発注しない（補充は約定側で行う）
        if self.initialized:
            need_buy_seed = len(self.placed_buy_px_to_id) == 0
            need_sell_seed = len(self.placed_sell_px_to_id) == 0
            # 片側が空なら再シード（初期の挟み込みを回復）
            if need_buy_seed or need_sell_seed:
                buy_targets = [float(mid_price) - (self.first_offset + i * self.step) for i in range(self.levels)]
                sell_targets = [float(mid_price) + (self.first_offset + i * self.step) for i in range(self.levels)]
                logger.info("再配置: need_buy={} need_sell={} P={} X={} N={}", need_buy_seed, need_sell_seed, mid_price, self.first_offset, self.step)
                # BUY再種まき
                if need_buy_seed:
                    new_buys = 0
                    for px in buy_targets:
                        if px <= 0:
                            continue
                        if px >= (mid_price - 1e-9):
                            continue
                        if px in self.placed_buy_px_to_id:
                            continue
                        if not self._has_min_gap(self.placed_buy_px_to_id, px):
                            continue
                        await self._place_order(OrderSide.BUY, px)
                        new_buys += 1
                        await asyncio.sleep(self.op_spacing_sec)
                        if new_buys >= self.levels:
                            break
                # SELL再種まき
                if need_sell_seed:
                    new_sells = 0
                    for px in sell_targets:
                        if px <= (mid_price + 1e-9):
                            continue
                        if px in self.placed_sell_px_to_id:
                            continue
                        if not self._has_min_gap(self.placed_sell_px_to_id, px):
                            continue
                        await self._place_order(OrderSide.SELL, px)
                        new_sells += 1
                        await asyncio.sleep(self.op_spacing_sec)
                        if new_sells >= self.levels:
                            break
                return

            # 両サイドに1本以上ある場合: 追従（価格乖離の自動シフト）
            # 任意: 価格追従（シンプルモードでは既定OFF）
            if self.follow_enable and self.step > 0:

                # BUY側: 近い買いが P-(X+slack*N) より遠くにあるなら、遠い買いを1本消して内側へ1ステップ寄せる
                try:
                    shifts = 0
                    if self.placed_buy_px_to_id:
                        nearest_buy = max(self.placed_buy_px_to_id.keys())  # 市場に最も近い買い
                        desired_min_buy = float(mid_price) - (self.first_offset + self.follow_slack_steps * self.step)
                        while nearest_buy < desired_min_buy - 1e-9 and shifts < self.max_shift_per_loop:
                            if len(self.placed_buy_px_to_id) <= 0:
                                break
                            far_buy_px = min(self.placed_buy_px_to_id.keys())
                            far_buy_id = self.placed_buy_px_to_id.pop(far_buy_px)
                            try:
                                await self.adapter.cancel_order(far_buy_id)
                                logger.info("追従: 遠いBUYキャンセル px={}", far_buy_px)
                            except Exception:
                                logger.debug("追従: 遠いBUYキャンセル失敗(無視) id={} px={}", far_buy_id, far_buy_px)
                            await asyncio.sleep(self.op_spacing_sec)

                            new_buy_px = nearest_buy + self.step
                            # 安全: 現在価格の内側には置かない
                            if new_buy_px >= (mid_price - 1e-9):
                                break
                            if new_buy_px in self.placed_buy_px_to_id:
                                nearest_buy = new_buy_px
                                shifts += 1
                                continue
                            if not self._has_min_gap(self.placed_buy_px_to_id, new_buy_px):
                                logger.debug("追従: BUY gap違反でスキップ new_px={}", new_buy_px)
                                break
                            await self._place_order(OrderSide.BUY, new_buy_px)
                            nearest_buy = new_buy_px
                            shifts += 1
                            await asyncio.sleep(self.op_spacing_sec)
                        if shifts:
                            logger.debug("追従BUY: nearest={} desired_min={} shifts={}", nearest_buy, desired_min_buy, shifts)
                except Exception as e:
                    logger.debug("追従BUY処理スキップ: {}", e)

                # SELL側: 近い売りが P+(X+slack*N) より遠くにあるなら、遠い売りを1本消して内側へ1ステップ寄せる
                try:
                    shifts = 0
                    if self.placed_sell_px_to_id:
                        nearest_sell = min(self.placed_sell_px_to_id.keys())  # 市場に最も近い売り
                        desired_max_sell = float(mid_price) + (self.first_offset + self.follow_slack_steps * self.step)
                        while nearest_sell > desired_max_sell + 1e-9 and shifts < self.max_shift_per_loop:
                            if len(self.placed_sell_px_to_id) <= 0:
                                break
                            far_sell_px = max(self.placed_sell_px_to_id.keys())
                            far_sell_id = self.placed_sell_px_to_id.pop(far_sell_px)
                            try:
                                await self.adapter.cancel_order(far_sell_id)
                                logger.info("追従: 遠いSELLキャンセル px={}", far_sell_px)
                            except Exception:
                                logger.debug("追従: 遠いSELLキャンセル失敗(無視) id={} px={}", far_sell_id, far_sell_px)
                            await asyncio.sleep(self.op_spacing_sec)

                            new_sell_px = nearest_sell - self.step
                            # 安全: 現在価格の内側には置かない
                            if new_sell_px <= (mid_price + 1e-9):
                                break
                            if new_sell_px in self.placed_sell_px_to_id:
                                nearest_sell = new_sell_px
                                shifts += 1
                                continue
                            if not self._has_min_gap(self.placed_sell_px_to_id, new_sell_px):
                                logger.debug("追従: SELL gap違反でスキップ new_px={}", new_sell_px)
                                break
                            await self._place_order(OrderSide.SELL, new_sell_px)
                            nearest_sell = new_sell_px
                            shifts += 1
                            await asyncio.sleep(self.op_spacing_sec)
                        if shifts:
                            logger.debug("追従SELL: nearest={} desired_max={} shifts={}", nearest_sell, desired_max_sell, shifts)
                except Exception as e:
                    logger.debug("追従SELL処理スキップ: {}", e)
            # フォローの有無に関係なく、本数不足があれば外側に補充（levels維持）
            try:
                # 片側あたりの新規上限を考慮
                add_buys = 0
                add_sells = 0
                # BUY不足: 最外側(min)から外側へ足す（失敗時はさらに一段外へ最大3回リトライ）
                while len(self.placed_buy_px_to_id) < self.levels:
                    if not self.placed_buy_px_to_id:
                        break
                    cand = min(self.placed_buy_px_to_id.keys()) - self.step
                    attempts = 0
                    placed = False
                    while cand <= (mid_price - 1e-9) and self._has_min_gap(self.placed_buy_px_to_id, cand) and attempts < 3:
                        if self.max_new_per_loop and add_buys >= self.max_new_per_loop:
                            break
                        before = set(self.placed_buy_px_to_id.keys())
                        await self._place_order(OrderSide.BUY, cand)
                        await asyncio.sleep(self.op_spacing_sec)
                        after = set(self.placed_buy_px_to_id.keys())
                        if cand in after and cand not in before:
                            placed = True
                            add_buys += 1
                            break
                        # 価格が食い込み等で弾かれた場合はさらに外側へ
                        cand -= self.step
                        attempts += 1
                    if not placed:
                        break
                # SELL不足: 最外側(max)から外側へ足す（失敗時はさらに一段外へ最大3回リトライ）
                while len(self.placed_sell_px_to_id) < self.levels:
                    if not self.placed_sell_px_to_id:
                        break
                    cand = max(self.placed_sell_px_to_id.keys()) + self.step
                    attempts = 0
                    placed = False
                    while cand >= (mid_price + 1e-9) and self._has_min_gap(self.placed_sell_px_to_id, cand) and attempts < 3:
                        if self.max_new_per_loop and add_sells >= self.max_new_per_loop:
                            break
                        before = set(self.placed_sell_px_to_id.keys())
                        await self._place_order(OrderSide.SELL, cand)
                        await asyncio.sleep(self.op_spacing_sec)
                        after = set(self.placed_sell_px_to_id.keys())
                        if cand in after and cand not in before:
                            placed = True
                            add_sells += 1
                            break
                        # 価格が食い込み等で弾かれた場合はさらに外側へ
                        cand += self.step
                        attempts += 1
                    if not placed:
                        break
                if add_buys or add_sells:
                    logger.debug("levels補充: add_buys={} add_sells={} now buy={} sell={}", add_buys, add_sells, len(self.placed_buy_px_to_id), len(self.placed_sell_px_to_id))
            except Exception as e:
                logger.debug("levels補充スキップ: {}", e)
            return
            # 上記でreturnしているため以降は不要な重複ロジックを削除

        # 候補を作る
        buy_targets = [float(mid_price) - (self.first_offset + i * self.step) for i in range(self.levels)]
        sell_targets = [float(mid_price) + (self.first_offset + i * self.step) for i in range(self.levels)]
        logger.debug("ensure(init): P={} X={} N={} buy_targets={} sell_targets={}", mid_price, self.first_offset, self.step, buy_targets, sell_targets)

        # 以降はターゲットに合わせて一斉キャンセルは行わない（アンカー方式）

        # 片側あたり新規上限が設定されていれば適用
        new_buys = 0
        new_sells = 0

        # 買い配置（P−X より内側は生成しない設計だが、念のためチェック）
        for px in buy_targets:
            if px <= 0:
                continue
            if px >= (mid_price - 1e-9):
                logger.debug("skip(init BUY): inside X (px={} >= P)", px)
                continue
            if px in self.placed_buy_px_to_id:
                logger.debug("skip(init BUY): already placed px={}", px)
                continue
            if not self._has_min_gap(self.placed_buy_px_to_id, px):
                logger.debug("skip(init BUY): gap < N at px={}", px)
                continue
            if self.max_new_per_loop and new_buys >= self.max_new_per_loop:
                break
            await self._place_order(OrderSide.BUY, px)
            new_buys += 1
            await asyncio.sleep(self.op_spacing_sec)
            
        # 売り配置（P＋X より内側は生成しない設計だが、念のためチェック）
        for px in sell_targets:
            if px in self.placed_sell_px_to_id:
                logger.debug("skip(init SELL): already placed px={}", px)
                continue
            if not self._has_min_gap(self.placed_sell_px_to_id, px):
                logger.debug("skip(init SELL): gap < N at px={}", px)
                continue
            if px <= (mid_price + 1e-9):
                logger.debug("skip(init SELL): inside X (px={} <= P)", px)
                continue
            if self.max_new_per_loop and new_sells >= self.max_new_per_loop:
                break
            await self._place_order(OrderSide.SELL, px)
            new_sells += 1
            await asyncio.sleep(self.op_spacing_sec)
            
        if not self.initialized:
            self.initialized = True
            logger.info("初回グリッド配置完了: 買い{}本 売り{}本", 
                       len(self.placed_buy_px_to_id), len(self.placed_sell_px_to_id))

    def _get_current_position_size_and_side(self) -> tuple[float, str | None]:
        """WebSocketから現在のポジションサイズと方向を取得

        Returns:
            tuple[float, str | None]: (絶対サイズ, 方向 "LONG"/"SHORT"/None)
        """
        if not hasattr(self.adapter, '_ws_client_private') or self.adapter._ws_client_private is None:
            return 0.0, None

        all_positions = self.adapter._ws_client_private.all_positions
        if not all_positions:
            return 0.0, None

        total_size = 0.0
        for position in all_positions:
            open_size_str = position.get("openSize")
            if open_size_str is None:
                continue
            size = float(open_size_str)
            if abs(size) < 0.0001:
                continue
            total_size += size

        if abs(total_size) < 0.0001:
            return 0.0, None

        pos_side = "LONG" if total_size > 0 else "SHORT"
        return abs(total_size), pos_side

    async def _place_order(self, side: OrderSide, price: float, order_type: OrderType = OrderType.LIMIT):
        """注文を発注

        Args:
            side: 注文サイド (BUY/SELL)
            price: 価格 (MARKET注文の場合は無視される)
            order_type: 注文タイプ (デフォルト: LIMIT)
        """
        # ポジションサイズ制限チェック (BTC絶対値 または RATIO)
        has_btc_limit = self.position_size_limit > 0 or self.position_size_reduce_only > 0
        has_ratio_limit = self.position_ratio_limit > 0 or self.position_ratio_reduce_only > 0

        if has_btc_limit or has_ratio_limit:
            pos_size, pos_side = self._get_current_position_size_and_side()

            # === BTC絶対値による判定 ===
            if has_btc_limit:
                # REDUCE_MODEの判定
                if self.position_size_limit > 0 and pos_size >= self.position_size_limit:
                    if not self._reduce_mode:
                        logger.warning(
                            "REDUCE_MODE突入(BTC): ポジションサイズ {:.4f} BTC >= 上限 {:.4f} BTC",
                            pos_size, self.position_size_limit
                        )
                        self._reduce_mode = True
                        # ポジション積み増し方向の既存注文をキャンセル
                        if pos_side is not None:
                            await self._cancel_position_side_orders(pos_side)

                # REDUCE_MODE解除判定
                if self._reduce_mode and self.position_size_reduce_only > 0:
                    if pos_size < self.position_size_reduce_only:
                        logger.warning(
                            "REDUCE_MODE解除(BTC): ポジションサイズ {:.4f} BTC < 閾値 {:.4f} BTC",
                            pos_size, self.position_size_reduce_only
                        )
                        self._reduce_mode = False

            # === RATIO (総資産比率) による判定 ===
            if has_ratio_limit and pos_size > 0:
                # 現在のBTC価格を取得
                btc_price = None
                if hasattr(self.adapter, 'get_current_price_from_websocket'):
                    btc_price = self.adapter.get_current_price_from_websocket()
                if btc_price is None or btc_price <= 0:
                    # WebSocketから取得できない場合はスキップ（RATIOチェックは行わない）
                    btc_price = None

                # 総資産（initial_asset）を取得
                initial_asset = None
                if (hasattr(self.adapter, '_ws_client_private') and
                    self.adapter._ws_client_private is not None and
                    hasattr(self.adapter._ws_client_private, 'initial_asset')):
                    initial_asset = self.adapter._ws_client_private.initial_asset

                if btc_price is not None and initial_asset is not None and initial_asset > 0:
                    # 式: (現在BTC価格 * ポジションサイズ) / 総資産
                    position_value_usd = btc_price * pos_size
                    current_ratio = position_value_usd / initial_asset

                    # REDUCE_MODEの判定
                    if self.position_ratio_limit > 0 and current_ratio >= self.position_ratio_limit:
                        if not self._reduce_mode:
                            logger.warning(
                                "REDUCE_MODE突入(RATIO): {:.4f} >= 上限 {:.4f} (pos={:.4f}BTC, price={:.0f}USD, asset={:.0f}USD)",
                                current_ratio, self.position_ratio_limit, pos_size, btc_price, initial_asset
                            )
                            self._reduce_mode = True
                            # ポジション積み増し方向の既存注文をキャンセル
                            if pos_side is not None:
                                await self._cancel_position_side_orders(pos_side)

                    # REDUCE_MODE解除判定
                    if self._reduce_mode and self.position_ratio_reduce_only > 0:
                        if current_ratio < self.position_ratio_reduce_only:
                            logger.warning(
                                "REDUCE_MODE解除(RATIO): {:.4f} < 閾値 {:.4f} (pos={:.4f}BTC, price={:.0f}USD, asset={:.0f}USD)",
                                current_ratio, self.position_ratio_reduce_only, pos_size, btc_price, initial_asset
                            )
                            self._reduce_mode = False

            # REDUCE_MODEでポジション積み増し方向の注文をスキップ
            if self._reduce_mode and pos_side is not None:
                # LONG保持中にBUY → 積み増し → スキップ
                # SHORT保持中にSELL → 積み増し → スキップ
                is_increasing = (
                    (pos_side == "LONG" and side == OrderSide.BUY) or
                    (pos_side == "SHORT" and side == OrderSide.SELL)
                )
                if is_increasing:
                    logger.debug(
                        "REDUCE_MODE: ポジション積み増し注文をスキップ side={} pos_side={} pos_size={:.4f}",
                        side, pos_side, pos_size
                    )
                    return

        # スケジュールのlot_coefficientを適用
        lot_coeff = self.schedule_manager.get_lot_coefficient()
        if lot_coeff <= 0:
            lot_coeff = 1.0
        quantity = self.size * lot_coeff

        if order_type == OrderType.MARKET:
            # MARKET注文: price=0, time_in_forceは設定しない
            req = OrderRequest(
                symbol=self.symbol,
                side=side,
                type=OrderType.MARKET,
                quantity=quantity,
                price=0,
            )
        else:
            # LIMIT注文: priceとPOST_ONLYを設定
            req = OrderRequest(
                symbol=self.symbol,
                side=side,
                type=OrderType.LIMIT,
                quantity=quantity,
                price=price,
                time_in_force=TimeInForce.POST_ONLY  # ← MAKER注文（手数料リベート）
            )
        
        try:
            # シンプルモード: 取引所全体の同サイドOPENとの距離チェックを省略（高速化）
            if not self.simple_mode:
                try:
                    active = await self.adapter.list_active_orders(self.symbol)
                except Exception:
                    active = []
                # 候補と既存価格の距離がN未満ならスキップ
                def _extract_px(row: dict) -> float | None:
                    try:
                        raw = row.get("price") or row.get("px") or row.get("0")
                        return float(raw) if raw is not None else None
                    except Exception:
                        return None
                for row in (active or []):
                    if not isinstance(row, dict):
                        continue
                    # サイド判定（無ければスキップ）
                    s = str(row.get("side") or row.get("orderSide") or "").upper()
                    if (side == OrderSide.BUY and s not in ("BUY", "LONG")) or (side == OrderSide.SELL and s not in ("SELL", "SHORT")):
                        continue
                    apx = _extract_px(row)
                    if apx is None:
                        continue
                    if abs(apx - price) < (self.step - 1e-9):
                        logger.debug("N間隔未満のためスキップ: side={} cand={} exist={}", side, price, apx)
                        return

            # 自己クロス防止: 反対サイドに同値があればスキップ
            if side == OrderSide.BUY and price in self.placed_sell_px_to_id:
                logger.debug("自己クロス回避: BUYをスキップ 価格=${:.1f}", price)
                self._self_cross_skip_count += 1
                self._check_and_clear_on_excessive_skips()
                return
            if side == OrderSide.SELL and price in self.placed_buy_px_to_id:
                logger.debug("自己クロス回避: SELLをスキップ 価格=${:.1f}", price)
                self._self_cross_skip_count += 1
                self._check_and_clear_on_excessive_skips()
                return
            order = await self.adapter.place_order(req)
            if side == OrderSide.BUY:
                self.placed_buy_px_to_id[price] = order.id
                self._add_to_cache(order.id, "BUY", price)
                logger.info("買い注文発注: 価格=${:.1f} ID={}", price, order.id)
            else:
                self.placed_sell_px_to_id[price] = order.id
                self._add_to_cache(order.id, "SELL", price)
                logger.info("売り注文発注: 価格=${:.1f} ID={}", price, order.id)
        except Exception as e:
            logger.error("注文発注エラー: side={} price={} error={}", side, price, e)

    async def _replenish_if_filled(self):
        """約定した注文を確認し、補充する（キャッシュを使用）"""
        # BIN固定モードでは、約定イベントに依存せず ensure_grid が目標集合に揃えるためスキップ
        if getattr(self, "bin_mode", False):
            return
        try:
            # キャッシュを使用（API呼び出し削減）
            active_orders = self._cached_active_orders
            # EdgeXアダプタは dict を返すため堅牢にIDを抽出する
            active_ids = set()
            for o in active_orders:
                try:
                    if isinstance(o, dict):
                        oid = (
                            o.get("orderId")
                            or o.get("id")
                            or o.get("order_id")
                            or o.get("clientOrderId")
                            or o.get("client_order_id")
                        )
                    else:
                        oid = getattr(o, "id", None) or getattr(o, "orderId", None)
                    if oid:
                        active_ids.add(str(oid))
                except Exception:
                    continue
            
            # 買い注文の約定確認
            filled_buy_prices = []
            for px, oid in list(self.placed_buy_px_to_id.items()):
                if oid not in active_ids:
                    logger.info("買い注文約定: 価格=${:.1f} ID={}", px, oid)
                    filled_buy_prices.append(px)
            
            # 売り注文の約定確認
            filled_sell_prices = []
            for px, oid in list(self.placed_sell_px_to_id.items()):
                if oid not in active_ids:
                    logger.info("売り注文約定: 価格=${:.1f} ID={}", px, oid)
                    filled_sell_prices.append(px)
            
            # 約定した注文を削除
            for px in filled_buy_prices:
                del self.placed_buy_px_to_id[px]
            for px in filled_sell_prices:
                del self.placed_sell_px_to_id[px]
            
            if filled_buy_prices or filled_sell_prices:
                logger.info("約定確認完了: 買い{}本 売り{}本", 
                           len(filled_buy_prices), len(filled_sell_prices))

            # === アンカー方式の補充ロジック ===
            # BUYが約定した場合: 
            #  - 反対側(SELL)の一番遠い指値(最大価格)を1つキャンセル
            #  - SELLを一番近い側に1つ追加（現在の最安SELLよりNだけ内側=より近い価格）
            #  - BUYを一番外側（現在の最安BUYよりNだけ外側=より安い価格）に1つ追加
            if filled_buy_prices:
                # 反対側の一番遠いSELLをキャンセル
                if self.placed_sell_px_to_id:
                    far_sell_px = max(self.placed_sell_px_to_id.keys())
                    far_sell_id = self.placed_sell_px_to_id.pop(far_sell_px)
                    try:
                        await self.adapter.cancel_order(far_sell_id)
                        self._remove_from_cache(far_sell_id)
                    except Exception:
                        logger.debug("cancel far SELL failed (ignore): id={} px={}", far_sell_id, far_sell_px)
                    await asyncio.sleep(self.op_spacing_sec)
                # SELLを一番近い側に追加
                base_near_sell = min(self.placed_sell_px_to_id.keys()) if self.placed_sell_px_to_id else (max(filled_buy_prices) + self.step)
                new_near_sell = base_near_sell - self.step
                logger.debug("replenish BUY: near_sell_base={} -> new_near_sell={} outer_buy_base(current)={}", base_near_sell, new_near_sell, min(self.placed_buy_px_to_id.keys()) if self.placed_buy_px_to_id else None)
                if new_near_sell not in self.placed_sell_px_to_id and new_near_sell > 0:
                    await self._place_order(OrderSide.SELL, new_near_sell)
                    await asyncio.sleep(self.op_spacing_sec)
                # BUYを一番外側に追加
                base_outer_buy = min(self.placed_buy_px_to_id.keys()) if self.placed_buy_px_to_id else (min(filled_buy_prices) - self.step)
                new_outer_buy = base_outer_buy - self.step
                logger.debug("replenish BUY: base_outer_buy={} -> new_outer_buy={}", base_outer_buy, new_outer_buy)
                if new_outer_buy > 0 and new_outer_buy not in self.placed_buy_px_to_id:
                    await self._place_order(OrderSide.BUY, new_outer_buy)
                    await asyncio.sleep(self.op_spacing_sec)

            # SELLが約定した場合:
            #  - 反対側(BUY)の一番遠い指値(最小価格)を1つキャンセル
            #  - BUYを一番近い側に1つ追加（現在の最高BUYよりNだけ内側=より高い価格）
            #  - SELLを一番外側（現在の最高SELLよりNだけ外側=より高い価格）に1つ追加
            if filled_sell_prices:
                # 反対側の一番遠いBUYをキャンセル
                if self.placed_buy_px_to_id:
                    far_buy_px = min(self.placed_buy_px_to_id.keys())
                    far_buy_id = self.placed_buy_px_to_id.pop(far_buy_px)
                    try:
                        await self.adapter.cancel_order(far_buy_id)
                        self._remove_from_cache(far_buy_id)
                    except Exception:
                        logger.debug("cancel far BUY failed (ignore): id={} px={}", far_buy_id, far_buy_px)
                    await asyncio.sleep(self.op_spacing_sec)
                # BUYを一番近い側に追加
                base_near_buy = max(self.placed_buy_px_to_id.keys()) if self.placed_buy_px_to_id else (min(filled_sell_prices) - self.step)
                new_near_buy = base_near_buy + self.step
                logger.debug("replenish SELL: near_buy_base={} -> new_near_buy={} outer_sell_base(current)={}", base_near_buy, new_near_buy, max(self.placed_sell_px_to_id.keys()) if self.placed_sell_px_to_id else None)
                if new_near_buy not in self.placed_buy_px_to_id and new_near_buy > 0:
                    await self._place_order(OrderSide.BUY, new_near_buy)
                    await asyncio.sleep(self.op_spacing_sec)
                # SELLを一番外側に追加
                base_outer_sell = max(self.placed_sell_px_to_id.keys()) if self.placed_sell_px_to_id else (max(filled_sell_prices) + self.step)
                new_outer_sell = base_outer_sell + self.step
                logger.debug("replenish SELL: base_outer_sell={} -> new_outer_sell={}", base_outer_sell, new_outer_sell)
                if new_outer_sell not in self.placed_sell_px_to_id:
                    await self._place_order(OrderSide.SELL, new_outer_sell)
                    await asyncio.sleep(self.op_spacing_sec)
        
        except Exception as e:
            logger.error("約定確認エラー: {}", e)
            return

        # 余剰オーダーの整理（このBotが出していないOPEN注文を徐々に解消）
        if self.enforce_levels:
            try:
                placed_ids = set(self.placed_buy_px_to_id.values()) | set(self.placed_sell_px_to_id.values())
                # 抽出関数
                def _oid(row: dict) -> str:
                    return str(row.get("orderId") or row.get("id") or row.get("order_id") or "")
                # 未管理のOPEN注文
                unknown = []
                for row in (active_orders or []):
                    if not isinstance(row, dict):
                        continue
                    oid = _oid(row)
                    if not oid or oid in placed_ids:
                        continue
                    status = str(row.get("status") or "").upper()
                    if status and status != "OPEN":
                        continue
                    unknown.append(oid)
                # 1ループで最大3件だけキャンセルし、徐々に整理
                for oid in unknown[:3]:
                    try:
                        await self.adapter.cancel_order(oid)
                        self._remove_from_cache(oid)
                        logger.info("余剰注文をキャンセル: id={}", oid)
                    except Exception:
                        logger.debug("余剰注文キャンセル失敗(無視): id={}", oid)
                    await asyncio.sleep(self.op_spacing_sec)
            except Exception as e:
                logger.debug("余剰整理スキップ: {}", e)

    async def _poll_closed_pnl_once(self):
        """定期的にクローズ済みPnLを取得"""
        if self.closed_poll_sec <= 0:
            return
        
        now = time.time()
        if now - self._last_closed_poll_ts < self.closed_poll_sec:
            return
        
        self._last_closed_poll_ts = now
        
        try:
            # ここでクローズ済みPnLを取得する処理を実装
            # 現在は未実装のため、スキップ
            pass
        except Exception as e:
            logger.error("クローズ済みPnL取得エラー: {}", e)

    async def _handle_schedule_exit(self) -> None:
        """スケジュール外に出た時の処理

        環境変数 EDGEX_OUT_OF_SCHEDULE_ACTION で挙動を制御:
        - "nothing": 全指値注文をキャンセルし、ポジションはそのまま維持して終了
        - "auto" または未設定: 指値でクローズを試み、1分後に約定していなければ成行でクローズ
        - "immediately": 即時成行でクローズ
        """
        action = os.getenv("EDGEX_OUT_OF_SCHEDULE_ACTION", "auto").lower().strip()

        logger.warning("=" * 80)
        logger.warning("SCHEDULE ENDED (action={})", action)
        logger.warning("=" * 80)

        try:
            # 全注文をキャンセル（全アクション共通）
            logger.warning("Canceling all open orders...")
            active_orders = await self.adapter.list_active_orders(self.symbol)
            cancel_count = 0
            for order in active_orders:
                try:
                    order_id = (
                        order.get("orderId")
                        or order.get("id")
                        or order.get("order_id")
                        or order.get("clientOrderId")
                    )
                    if order_id:
                        await self.adapter.cancel_order(str(order_id))
                        cancel_count += 1
                        await asyncio.sleep(0.1)
                except Exception as e:
                    logger.debug(f"Failed to cancel order {order_id}: {e}")
            logger.warning(f"Canceled {cancel_count} open orders")

            # 内部トラッキングをクリア
            self.placed_buy_px_to_id.clear()
            self.placed_sell_px_to_id.clear()
            self._cached_active_orders = []
            self.initialized = False

            if action == "nothing":
                # ポジションは触らず終了
                logger.warning("EDGEX_OUT_OF_SCHEDULE_ACTION=nothing: 注文キャンセル完了、ポジションは維持")
            elif action == "immediately":
                # 即時成行でクローズ
                logger.warning("EDGEX_OUT_OF_SCHEDULE_ACTION=immediately: 成行で即時クローズ")
                if hasattr(self.adapter, 'close_position_from_websocket'):
                    closed = await self.adapter.close_position_from_websocket(self.symbol)
                    if closed:
                        logger.warning("Position closed with market order")
                    else:
                        logger.info("No position to close")
            else:
                # auto: 指値でクローズを試み、1分後に約定していなければ成行
                logger.warning("EDGEX_OUT_OF_SCHEDULE_ACTION=auto: 指値でクローズ試行（1分後に成行フォールバック）")
                await self._close_position_with_limit_then_market()

        except Exception as e:
            logger.error(f"Error during schedule exit handling: {e}", exc_info=True)

        logger.warning("Schedule exit handling complete")

    async def _close_position_with_limit_then_market(self) -> None:
        """指値でポジションクローズを試み、1分後に約定していなければ成行でクローズ"""
        if not hasattr(self.adapter, '_ws_client_private') or self.adapter._ws_client_private is None:
            logger.warning("WebSocket client not available - falling back to market order")
            if hasattr(self.adapter, 'close_position_from_websocket'):
                await self.adapter.close_position_from_websocket(self.symbol)
            return

        # WebSocketからポジション情報を取得
        all_positions = self.adapter._ws_client_private.all_positions
        if not all_positions:
            logger.info("No position data from WebSocket")
            return

        # ポジションサイズを計算
        total_size = 0.0
        for position in all_positions:
            open_size_str = position.get("openSize")
            if open_size_str is None:
                continue
            size = float(open_size_str)
            if abs(size) < 0.0001:
                continue
            total_size += size

        if abs(total_size) < 0.0001:
            logger.info("Total position size is zero - nothing to close")
            return

        # クローズ方向と価格を決定
        abs_total_size = abs(total_size)
        if total_size > 0:
            # LONG → SELLでクローズ
            close_side = OrderSide.SELL
            side_name = "LONG"
        else:
            # SHORT → BUYでクローズ
            close_side = OrderSide.BUY
            side_name = "SHORT"

        # 現在価格を取得
        mid_price = None
        if hasattr(self.adapter, 'get_current_price_from_websocket'):
            mid_price = self.adapter.get_current_price_from_websocket()
        if mid_price is None:
            ticker = await self.adapter.get_ticker(self.symbol)
            mid_price = float(ticker.price)

        # 指値価格: 現在価格から5ドル有利な価格
        if close_side == OrderSide.SELL:
            limit_price = mid_price + 5.0  # 売りは高めに
        else:
            limit_price = mid_price - 5.0  # 買いは安めに

        logger.warning(f"Placing limit order to close {side_name} position: size={abs_total_size}, price={limit_price}")

        # 指値注文を発注
        limit_order = OrderRequest(
            symbol=self.symbol,
            side=close_side,
            type=OrderType.LIMIT,
            quantity=abs_total_size,
            price=limit_price,
            time_in_force=TimeInForce.GTC,
        )

        try:
            result = await self.adapter.place_order(limit_order)
            limit_order_id = result.id
            logger.warning(f"Limit close order placed: {limit_order_id}")
        except Exception as e:
            logger.error(f"Failed to place limit close order: {e}")
            # 指値が失敗したら即成行
            if hasattr(self.adapter, 'close_position_from_websocket'):
                await self.adapter.close_position_from_websocket(self.symbol)
            return

        # 1分待機
        logger.info("Waiting 60 seconds for limit order to fill...")
        await asyncio.sleep(60)

        # 注文がまだアクティブか確認
        try:
            active_orders = await self.adapter.list_active_orders(self.symbol)
            order_still_active = False
            for order in active_orders:
                order_id = (
                    order.get("orderId")
                    or order.get("id")
                    or order.get("order_id")
                    or order.get("clientOrderId")
                )
                if str(order_id) == str(limit_order_id):
                    order_still_active = True
                    break

            if order_still_active:
                # まだ約定していない → キャンセルして成行
                logger.warning("Limit order not filled after 60s - canceling and using market order")
                try:
                    await self.adapter.cancel_order(limit_order_id)
                except Exception:
                    pass
                await asyncio.sleep(0.5)

                if hasattr(self.adapter, 'close_position_from_websocket'):
                    closed = await self.adapter.close_position_from_websocket(self.symbol)
                    if closed:
                        logger.warning("Position closed with market order (fallback)")
            else:
                logger.info("Limit close order filled successfully")

        except Exception as e:
            logger.error(f"Error checking limit order status: {e}")
            # エラー時は念のため成行でクローズ試行
            if hasattr(self.adapter, 'close_position_from_websocket'):
                await self.adapter.close_position_from_websocket(self.symbol)

    def _periodic_clear_placed_maps(self) -> None:
        """1時間に1回、placed_buy/sell_px_to_idとスキップカウントをクリア"""
        now = time.time()

        # placed_buy/sell_px_to_idの定期クリア
        if now - self._last_placed_clear_ts >= self._placed_clear_interval_sec:
            buy_count = len(self.placed_buy_px_to_id)
            sell_count = len(self.placed_sell_px_to_id)
            if buy_count > 0 or sell_count > 0:
                logger.info(
                    "定期クリア(1時間): placed_buy={}件, placed_sell={}件 をクリア",
                    buy_count, sell_count
                )
            self.placed_buy_px_to_id.clear()
            self.placed_sell_px_to_id.clear()
            self._cached_active_orders = []
            self._last_placed_clear_ts = now

        # スキップカウントの定期クリア
        if now - self._last_skip_clear_ts >= self._skip_clear_interval_sec:
            if self._self_cross_skip_count > 0:
                logger.info(
                    "定期クリア(1時間): 自己クロススキップカウント={}件 をリセット",
                    self._self_cross_skip_count
                )
            self._self_cross_skip_count = 0
            self._last_skip_clear_ts = now

    def _check_and_clear_on_excessive_skips(self) -> None:
        """自己クロススキップが閾値を超えたらplaced_mapをクリア

        閾値: 1分間にGRID_LEVELS_PER_SIDEの3倍
        """
        threshold = self.levels * 3
        if self._self_cross_skip_count >= threshold:
            logger.warning(
                "自己クロススキップ過多({}>={})により強制クリア: placed_buy={}件, placed_sell={}件",
                self._self_cross_skip_count,
                threshold,
                len(self.placed_buy_px_to_id),
                len(self.placed_sell_px_to_id)
            )
            self.placed_buy_px_to_id.clear()
            self.placed_sell_px_to_id.clear()
            self._cached_active_orders = []
            self._self_cross_skip_count = 0
            self._last_skip_clear_ts = time.time()
# touch test 2025-11-01T23:11:35
