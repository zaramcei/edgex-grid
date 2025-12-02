import asyncio
import binascii
import json
import logging
import os
import threading
import time
from typing import Dict, Any, List, Optional, Callable, Union

import websocket
from Crypto.Hash import keccak

from ..internal.signing_adapter import SigningAdapter

from ..internal.client import Client as InternalClient

# Leverage for unrealized PnL calculation (configurable via environment variable)
def _get_leverage() -> float:
    """Get leverage from environment variable or use default"""
    try:
        return float(os.getenv("EDGEX_LEVERAGE", "100.0"))
    except ValueError:
        return 100.0

# Loss cut percentage threshold (configurable via environment variable)
def _get_losscut_percentage() -> Optional[float]:
    """Get loss cut percentage from environment variable"""
    try:
        val = os.getenv("EDGEX_POSITION_LOSSCUT_PERCENTAGE")
        if val is None:
            return None
        return float(val)
    except ValueError:
        return None

# Take profit percentage threshold (configurable via environment variable)
def _get_takeprofit_percentage() -> Optional[float]:
    """Get take profit percentage from environment variable"""
    try:
        val = os.getenv("EDGEX_POSITION_TAKE_PROFIT_PERCENTAGE")
        if val is None:
            return None
        return float(val)
    except ValueError:
        return None

# Initial balance for balance recovery feature (configurable via environment variable)
def _get_initial_balance() -> Optional[float]:
    """Get initial balance (USD) from environment variable"""
    try:
        val = os.getenv("EDGEX_INITIAL_BALANCE_USD")
        if val is None:
            return None
        return float(val)
    except ValueError:
        return None

# Balance recovery enabled flag (configurable via environment variable)
def _get_balance_recovery_enabled() -> bool:
    """Get balance recovery enabled flag from environment variable"""
    val = os.getenv("EDGEX_BALANCE_RECOVERY_ENABLED", "false").lower()
    return val in ("1", "true", "yes", "on")

# Minimum loss required before recovery mode can activate (configurable via environment variable)
def _get_recovery_enforce_level() -> float:
    """Get recovery enforce level (USD) from environment variable"""
    try:
        return float(os.getenv("EDGEX_RECOVERY_ENFORCE_LEVEL_USD", "10.0"))
    except ValueError:
        return 10.0

# Asset-based loss cut percentage threshold (configurable via environment variable)
def _get_asset_losscut_percentage() -> float:
    """Get asset-based loss cut percentage from environment variable (default: 0 = disabled)"""
    try:
        return float(os.getenv("EDGEX_ASSET_LOSSCUT_PERCENTAGE", "0"))
    except ValueError:
        return 0

# Asset-based take profit percentage threshold (configurable via environment variable)
def _get_asset_takeprofit_percentage() -> float:
    """Get asset-based take profit percentage from environment variable (default: 0 = disabled)"""
    try:
        return float(os.getenv("EDGEX_ASSET_TAKE_PROFIT_PERCENTAGE", "0"))
    except ValueError:
        return 0

LEVERAGE = _get_leverage()
LOSSCUT_PERCENTAGE = _get_losscut_percentage()
TAKEPROFIT_PERCENTAGE = _get_takeprofit_percentage()
INITIAL_BALANCE_USD = _get_initial_balance()
BALANCE_RECOVERY_ENABLED = _get_balance_recovery_enabled()
EDGEX_RECOVERY_ENFORCE_LEVEL_USD = _get_recovery_enforce_level()
ASSET_LOSSCUT_PERCENTAGE = _get_asset_losscut_percentage()
ASSET_TAKEPROFIT_PERCENTAGE = _get_asset_takeprofit_percentage()


class Client:
    """WebSocket client for real-time data."""

    def __init__(self, url: str, is_private: bool, account_id: int, stark_pri_key: str, signing_adapter: Optional[SigningAdapter] = None):
        """
        Initialize the WebSocket client.

        Args:
            url: WebSocket URL
            is_private: Whether this is a private WebSocket connection
            account_id: Account ID for authentication
            stark_pri_key: Stark private key for signing
        """
        self.url = url
        self.is_private = is_private
        self.account_id = account_id
        self.stark_pri_key = stark_pri_key

        # Use the provided signing adapter (required)
        if signing_adapter is None:
            raise ValueError("signing_adapter is required")
        self.signing_adapter = signing_adapter

        self.conn = None
        self.handlers = {}
        self.done = threading.Event()
        self.ping_thread = None
        self.subscriptions = set()
        self.on_connect_hooks = []
        self.on_message_hooks = []
        self.on_disconnect_hooks = []

        # Use loguru if available, otherwise fall back to standard logging
        try:
            from loguru import logger as loguru_logger
            self.logger = loguru_logger
        except ImportError:
            self.logger = logging.getLogger(__name__)

        # Position monitoring fields
        self.current_position: Optional[Dict[str, Any]] = None  # Primary position (for compatibility)
        self.all_positions: List[Dict[str, Any]] = []  # All positions from WebSocket
        self.current_price: Optional[float] = None
        self.position_monitoring_enabled = False
        self.losscut_triggered = False  # Flag indicating if loss cut condition was triggered
        self.balance_recovery_triggered = False  # Flag indicating if balance recovery was triggered
        self.current_balance: Optional[float] = None  # Current account balance in USD
        self.initial_asset: Optional[float] = None  # Initial asset (balance) for asset-based loss cut
        self.asset_losscut_triggered = False  # Flag indicating if asset-based loss cut was triggered
        self.asset_takeprofit_triggered = False  # Flag indicating if asset-based take profit was triggered

    def connect(self):
        """
        Establish a WebSocket connection.

        Raises:
            ValueError: If the connection fails
        """
        headers = {}
        url = self.url

        # Add timestamp parameter for both public and private connections
        timestamp = int(time.time() * 1000)

        if self.is_private:
            # Add timestamp header
            headers["X-edgeX-Api-Timestamp"] = str(timestamp)

            # Generate signature content (no ? separator, matching Go SDK)
            path = f"/api/v1/private/wsaccountId={self.account_id}"
            sign_content = f"{timestamp}GET{path}"

            # Hash the content
            keccak_hash = keccak.new(digest_bits=256)
            keccak_hash.update(sign_content.encode())
            message_hash = keccak_hash.digest()

            # Sign the message using the signing adapter
            try:
                r, s = self.signing_adapter.sign(message_hash, self.stark_pri_key)
            except Exception as e:
                raise ValueError(f"failed to sign message: {str(e)}")

            # Set signature header
            headers["X-edgeX-Api-Signature"] = f"{r}{s}"
        else:
            # For public connections, add timestamp as URL parameter
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}timestamp={timestamp}"

        # Create WebSocket connection
        try:
            self.logger.info(f"Connecting to WebSocket: {url}")
            self.conn = websocket.create_connection(url, header=headers)
            self.logger.info(f"WebSocket connection established")
        except Exception as e:
            raise ValueError(f"failed to connect to WebSocket: {str(e)}")

        # Start ping thread
        self.done.clear()
        self.ping_thread = threading.Thread(target=self._ping_loop)
        self.ping_thread.daemon = True
        self.ping_thread.start()
        self.logger.debug("Ping thread started")

        # Start message handling thread
        self.message_thread = threading.Thread(target=self._handle_messages)
        self.message_thread.daemon = True
        self.message_thread.start()
        self.logger.debug("Message handling thread started")

        # Call connect hooks
        for hook in self.on_connect_hooks:
            hook()

    def close(self):
        """Close the WebSocket connection."""
        self.done.set()

        if self.conn:
            self.conn.close()
            self.conn = None

    def _ping_loop(self):
        """Send periodic ping messages."""
        while not self.done.is_set():
            if self.conn:
                ping_msg = {
                    "type": "ping",
                    "time": str(int(time.time() * 1000))
                }

                try:
                    self.conn.send(json.dumps(ping_msg))
                except Exception as e:
                    self.logger.error(f"Failed to send ping: {str(e)}")
                    break

            # Wait for 30 seconds or until done
            self.done.wait(30)

    def _handle_messages(self):
        """Process incoming WebSocket messages."""
        while not self.done.is_set():
            if not self.conn:
                break

            try:
                message = self.conn.recv()

                # Call message hooks
                for hook in self.on_message_hooks:
                    hook(message)

                # Parse message
                try:
                    msg = json.loads(message)
                except json.JSONDecodeError:
                    self.logger.debug(f"Failed to parse message as JSON: {message[:100]}")
                    continue

                msg_type = msg.get("type", "")

                # Handle ping messages
                if msg_type == "ping":
                    self._handle_pong(msg.get("time", ""))
                    continue

                # Handle quote events
                if msg_type == "quote-event":
                    channel = msg.get("channel", "")
                    # self.logger.debug(f"Received quote-event for channel: {channel}")
                    channel_type = channel.split(".")[0] if "." in channel else channel

                    if channel_type in self.handlers:
                        self.handlers[channel_type](message)
                    else:
                        # self.logger.debug(f"No handler registered for channel type: {channel_type}")
                        pass
                    continue

                # Call registered handlers for other message types
                if msg_type in self.handlers:
                    self.handlers[msg_type](message)
                elif msg_type and msg_type != "pong":
                    self.logger.debug(f"No handler for message type: {msg_type}")

            except Exception as e:
                self.logger.error(f"Error handling message: {str(e)}")

                # Call disconnect hooks
                for hook in self.on_disconnect_hooks:
                    hook(e)

                break

    def _handle_pong(self, timestamp: str):
        """
        Send pong response to server ping.

        Args:
            timestamp: The timestamp from the ping message
        """
        pong_msg = {
            "type": "pong",
            "time": timestamp
        }

        try:
            self.conn.send(json.dumps(pong_msg))
        except Exception as e:
            self.logger.error(f"Failed to send pong: {str(e)}")

    def subscribe(self, topic: str, params: Dict[str, Any] = None) -> bool:
        """
        Subscribe to a topic (for public WebSocket).

        Args:
            topic: The topic to subscribe to
            params: Optional parameters for the subscription

        Returns:
            bool: Whether the subscription was successful

        Raises:
            ValueError: If the subscription fails
        """
        if self.is_private:
            raise ValueError("cannot subscribe on private WebSocket connection")

        if not self.conn:
            raise ValueError("WebSocket connection is not established")

        sub_msg = {
            "type": "subscribe",
            "channel": topic
        }

        if params:
            sub_msg.update(params)

        try:
            self.logger.info(f"Sending subscription request: {sub_msg}")
            self.conn.send(json.dumps(sub_msg))
            self.subscriptions.add(topic)
            self.logger.info(f"Successfully subscribed to: {topic}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to subscribe to {topic}: {str(e)}")
            raise ValueError(f"failed to subscribe: {str(e)}")

    def unsubscribe(self, topic: str) -> bool:
        """
        Unsubscribe from a topic (for public WebSocket).

        Args:
            topic: The topic to unsubscribe from

        Returns:
            bool: Whether the unsubscription was successful

        Raises:
            ValueError: If the unsubscription fails
        """
        if self.is_private:
            raise ValueError("cannot unsubscribe on private WebSocket connection")

        if not self.conn:
            raise ValueError("WebSocket connection is not established")

        unsub_msg = {
            "type": "unsubscribe",
            "channel": topic
        }

        try:
            self.conn.send(json.dumps(unsub_msg))
            self.subscriptions.discard(topic)
            return True
        except Exception as e:
            raise ValueError(f"failed to unsubscribe: {str(e)}")

    def on_message(self, msg_type: str, handler: Callable[[str], None]):
        """
        Register a handler for a specific message type.

        Args:
            msg_type: The message type to handle
            handler: The handler function
        """
        self.handlers[msg_type] = handler

    def on_message_hook(self, hook: Callable[[str], None]):
        """
        Register a hook that will be called for all messages.

        Args:
            hook: The hook function
        """
        self.on_message_hooks.append(hook)

    def on_connect(self, hook: Callable[[], None]):
        """
        Register a hook that will be called when connection is established.

        Args:
            hook: The hook function
        """
        self.on_connect_hooks.append(hook)

    def on_disconnect(self, hook: Callable[[Exception], None]):
        """
        Register a hook that will be called when connection is closed.

        Args:
            hook: The hook function
        """
        self.on_disconnect_hooks.append(hook)

    def _calculate_and_log_unrealized_pnl(self) -> None:
        """Calculate and log unrealized PnL for all positions"""
        if not self.position_monitoring_enabled:
            self.logger.debug("Position monitoring not enabled")
            return

        if not self.all_positions:
            # self.logger.debug("No position data available")
            return

        if self.current_price is None:
            self.logger.warning(f"Current price not available (position exists but no ticker data yet)")
            return

        try:
            # Calculate total unrealized PnL across all positions
            total_unrealized_pnl = 0.0
            total_position_value = 0.0
            total_abs_size = 0.0
            has_valid_position = False
            combined_side = None

            for position in self.all_positions:
                # Extract position information
                open_size_str = position.get("openSize")
                open_value_str = position.get("openValue")

                if open_size_str is None or open_value_str is None:
                    continue

                size = float(open_size_str)
                open_value = float(open_value_str)

                # Skip if position is effectively zero
                if abs(size) < 0.0001:
                    continue

                has_valid_position = True

                # Determine side from openSize (positive = LONG, negative = SHORT)
                if size > 0:
                    side = "LONG"
                elif size < 0:
                    side = "SHORT"
                else:
                    continue

                # Set combined side (assume all positions are on same side)
                if combined_side is None:
                    combined_side = side

                # Calculate entry price from openValue / openSize
                entry_price = abs(open_value / size)

                # Calculate unrealized PnL for this position
                abs_size = abs(size)
                if side == "LONG":
                    # LONG: profit when price goes up
                    position_pnl = (self.current_price - entry_price) * abs_size
                else:
                    # SHORT: profit when price goes down
                    position_pnl = (entry_price - self.current_price) * abs_size

                # Accumulate totals
                total_unrealized_pnl += position_pnl
                total_position_value += entry_price * abs_size
                total_abs_size += abs_size

            # If no valid positions, reset flags and return
            if not has_valid_position:
                if self.losscut_triggered:
                    self.logger.info("All positions closed - resetting loss cut flag")
                    self.losscut_triggered = False
                if self.balance_recovery_triggered:
                    self.logger.info("All positions closed - resetting balance recovery flag")
                    self.balance_recovery_triggered = False
                # NOTE: Do NOT reset asset_losscut_triggered and asset_takeprofit_triggered here.
                # These flags must be reset explicitly by grid_engine after the losscut/takeprofit
                # processing is complete. If we reset them here, the grid_engine might miss the
                # trigger because the flag gets reset before grid_engine checks it.
                # The initial_asset will be updated by grid_engine after processing.
                return

            # Calculate total PnL percentage with leverage
            if total_position_value > 0:
                base_percentage = (total_unrealized_pnl / total_position_value) * 100
                pnl_percentage = base_percentage * LEVERAGE
            else:
                pnl_percentage = 0.0

            # Calculate average entry price
            avg_entry_price = total_position_value / total_abs_size if total_abs_size > 0 else 0.0

            # Log the results
            self.logger.info(
                f"Position Update | Side: {combined_side} | Total Size: {total_abs_size:.6f} | "
                f"Avg Entry: {avg_entry_price:.2f} | Current: {self.current_price:.2f} | "
                f"Total Unrealized PnL: {total_unrealized_pnl:+.6f} ({pnl_percentage:+.2f}% @ {LEVERAGE:.0f}x leverage)"
            )

            # Check loss cut condition
            if LOSSCUT_PERCENTAGE is not None and pnl_percentage <= -abs(LOSSCUT_PERCENTAGE):
                if not self.losscut_triggered:
                    self.losscut_triggered = True
                    self.logger.error("=" * 80)
                    self.logger.error(f"POSITION LOSS CUT TRIGGERED!")
                    self.logger.error(f"Current PnL: {pnl_percentage:+.2f}% | Threshold: -{abs(LOSSCUT_PERCENTAGE):.2f}%")
                    self.logger.error(f"Total Position: {combined_side} {total_abs_size:.6f} @ {avg_entry_price:.2f}")
                    self.logger.error(f"Total Unrealized Loss: {total_unrealized_pnl:+.6f}")
                    self.logger.error("=" * 80)

            # Check take profit condition
            if TAKEPROFIT_PERCENTAGE is not None and pnl_percentage >= abs(TAKEPROFIT_PERCENTAGE):
                if not self.losscut_triggered:  # Reuse losscut_triggered flag for take profit
                    self.losscut_triggered = True
                    self.logger.warning("=" * 80)
                    self.logger.warning(f"POSITION TAKE PROFIT TRIGGERED!")
                    self.logger.warning(f"Current PnL: {pnl_percentage:+.2f}% | Threshold: +{abs(TAKEPROFIT_PERCENTAGE):.2f}%")
                    self.logger.warning(f"Total Position: {combined_side} {total_abs_size:.6f} @ {avg_entry_price:.2f}")
                    self.logger.warning(f"Total Unrealized Profit: {total_unrealized_pnl:+.6f}")
                    self.logger.warning("=" * 80)

            # Check balance recovery condition
            if BALANCE_RECOVERY_ENABLED and INITIAL_BALANCE_USD is not None and self.current_balance is not None:
                # Calculate total balance (current balance + unrealized PnL)
                total_balance = self.current_balance + total_unrealized_pnl

                # Calculate how much balance decreased from initial
                balance_change = self.current_balance - INITIAL_BALANCE_USD
                recovery_amount = total_balance - INITIAL_BALANCE_USD

                # Calculate loss from initial balance
                loss_from_initial = INITIAL_BALANCE_USD - self.current_balance

                # Trigger if:
                # 1. There has been at least EDGEX_RECOVERY_ENFORCE_LEVEL_USD loss from initial balance
                # 2. AND total balance (including unrealized PnL) reaches or exceeds initial balance
                # This prevents premature triggering when balances are very close
                if loss_from_initial >= EDGEX_RECOVERY_ENFORCE_LEVEL_USD and total_balance >= INITIAL_BALANCE_USD:
                    if not self.balance_recovery_triggered:
                        self.balance_recovery_triggered = True
                        recovery_percentage = (recovery_amount / INITIAL_BALANCE_USD) * 100
                        self.logger.warning("=" * 80)
                        self.logger.warning(f"BALANCE RECOVERY TRIGGERED!")
                        self.logger.warning(f"Initial Balance: {INITIAL_BALANCE_USD:.2f} USD")
                        self.logger.warning(f"Current Balance: {self.current_balance:.2f} USD ({balance_change:+.2f} USD)")
                        self.logger.warning(f"Loss from Initial: {loss_from_initial:.2f} USD (threshold: {EDGEX_RECOVERY_ENFORCE_LEVEL_USD:.2f} USD)")
                        self.logger.warning(f"Total Unrealized PnL: {total_unrealized_pnl:+.2f} USD")
                        self.logger.warning(f"Total Balance: {total_balance:.2f} USD ({recovery_percentage:+.2f}%)")
                        self.logger.warning(f"Total Position: {combined_side} {total_abs_size:.6f} @ {avg_entry_price:.2f}")
                        self.logger.warning(f"Recovery complete - closing all positions to lock in profits")
                        self.logger.warning("=" * 80)

            # Check asset-based loss cut / take profit conditions
            # Either ASSET_LOSSCUT_PERCENTAGE or ASSET_TAKEPROFIT_PERCENTAGE being set enables this block
            if self.current_balance is not None and (ASSET_LOSSCUT_PERCENTAGE > 0 or ASSET_TAKEPROFIT_PERCENTAGE > 0):
                # Calculate total asset (current balance + unrealized PnL)
                total_asset = self.current_balance + total_unrealized_pnl

                # Record initial asset on first calculation (use current balance, not total asset)
                if self.initial_asset is None:
                    self.initial_asset = self.current_balance
                    self.logger.info(f"Initial asset recorded: {self.initial_asset:.2f} USD (current balance)")

                # Calculate loss percentage from initial asset
                if self.initial_asset is not None and self.initial_asset > 0:
                    asset_change = total_asset - self.initial_asset
                    asset_change_percentage = (asset_change / self.initial_asset) * 100

                    # Trigger loss cut if total asset drops below threshold
                    if ASSET_LOSSCUT_PERCENTAGE > 0 and asset_change_percentage <= -abs(ASSET_LOSSCUT_PERCENTAGE):
                        if not self.asset_losscut_triggered:
                            self.asset_losscut_triggered = True
                            self.logger.error("=" * 80)
                            self.logger.error(f"ASSET-BASED LOSS CUT TRIGGERED!")
                            self.logger.error(f"Initial Asset: {self.initial_asset:.2f} USD")
                            self.logger.error(f"Current Balance: {self.current_balance:.2f} USD")
                            self.logger.error(f"Total Unrealized PnL: {total_unrealized_pnl:+.2f} USD")
                            self.logger.error(f"Total Asset: {total_asset:.2f} USD ({asset_change_percentage:+.2f}%)")
                            self.logger.error(f"Loss Threshold: -{abs(ASSET_LOSSCUT_PERCENTAGE):.2f}%")
                            self.logger.error(f"Total Position: {combined_side} {total_abs_size:.6f} @ {avg_entry_price:.2f}")
                            self.logger.error(f"Closing all positions to prevent further loss")
                            self.logger.error("=" * 80)

                    # Trigger take profit if total asset exceeds threshold
                    if ASSET_TAKEPROFIT_PERCENTAGE > 0 and asset_change_percentage >= abs(ASSET_TAKEPROFIT_PERCENTAGE):
                        if not self.asset_takeprofit_triggered:
                            self.asset_takeprofit_triggered = True
                            self.logger.warning("=" * 80)
                            self.logger.warning(f"ASSET-BASED TAKE PROFIT TRIGGERED!")
                            self.logger.warning(f"Initial Asset: {self.initial_asset:.2f} USD")
                            self.logger.warning(f"Current Balance: {self.current_balance:.2f} USD")
                            self.logger.warning(f"Total Unrealized PnL: {total_unrealized_pnl:+.2f} USD")
                            self.logger.warning(f"Total Asset: {total_asset:.2f} USD ({asset_change_percentage:+.2f}%)")
                            self.logger.warning(f"Profit Threshold: +{abs(ASSET_TAKEPROFIT_PERCENTAGE):.2f}%")
                            self.logger.warning(f"Total Position: {combined_side} {total_abs_size:.6f} @ {avg_entry_price:.2f}")
                            self.logger.warning(f"Closing all positions to lock in profits")
                            self.logger.warning("=" * 80)

        except Exception as e:
            self.logger.error(f"Error calculating unrealized PnL: {str(e)}")

    def enable_position_monitoring(self) -> None:
        """Enable position monitoring and unrealized PnL calculation"""
        self.position_monitoring_enabled = True
        self.logger.info(f"Position monitoring enabled with leverage: {LEVERAGE:.0f}x")
        if LOSSCUT_PERCENTAGE is not None:
            self.logger.warning(f"Position loss cut enabled: position will be closed if PnL drops below -{abs(LOSSCUT_PERCENTAGE):.2f}%")
        else:
            self.logger.info("Position loss cut disabled (EDGEX_POSITION_LOSSCUT_PERCENTAGE not set)")

        if TAKEPROFIT_PERCENTAGE is not None:
            self.logger.warning(f"Position take profit enabled: position will be closed if PnL exceeds +{abs(TAKEPROFIT_PERCENTAGE):.2f}%")
        else:
            self.logger.info("Position take profit disabled (EDGEX_POSITION_TAKE_PROFIT_PERCENTAGE not set)")

        if BALANCE_RECOVERY_ENABLED:
            if INITIAL_BALANCE_USD is not None:
                self.logger.warning(f"Balance recovery enabled: Initial balance = {INITIAL_BALANCE_USD:.2f} USD")
                self.logger.warning(f"Minimum loss threshold: {EDGEX_RECOVERY_ENFORCE_LEVEL_USD:.2f} USD")
                self.logger.warning(f"Will close positions when total balance (current + unrealized) reaches initial balance after at least {EDGEX_RECOVERY_ENFORCE_LEVEL_USD:.2f} USD loss")
            else:
                self.logger.error("Balance recovery enabled but EDGEX_INITIAL_BALANCE_USD not set!")
        else:
            self.logger.info("Balance recovery disabled (EDGEX_BALANCE_RECOVERY_ENABLED not set)")

        if ASSET_LOSSCUT_PERCENTAGE > 0:
            self.logger.warning(f"Asset-based loss cut enabled: positions will be closed if total asset drops by -{abs(ASSET_LOSSCUT_PERCENTAGE):.2f}%")
        else:
            self.logger.info("Asset-based loss cut disabled (EDGEX_ASSET_LOSSCUT_PERCENTAGE = 0)")

        if ASSET_TAKEPROFIT_PERCENTAGE > 0:
            self.logger.warning(f"Asset-based take profit enabled: positions will be closed if total asset increases by +{abs(ASSET_TAKEPROFIT_PERCENTAGE):.2f}%")
        else:
            self.logger.info("Asset-based take profit disabled (EDGEX_ASSET_TAKE_PROFIT_PERCENTAGE = 0)")

        # Register handler for trade-event messages (contains position updates)
        def trade_event_handler(message: str) -> None:
            try:
                msg = json.loads(message)
                msg_type = msg.get("type")

                if msg_type != "trade-event":
                    return

                # self.logger.debug(f"Received trade-event message")
                content = msg.get("content", {})
                data = content.get("data", {})

                # Handle position updates first (to ensure positions are updated)
                position_list = data.get("position", [])
                if position_list:
                    self.logger.info(f"Position update received: {len(position_list)} positions")
                    # Store all positions
                    self.all_positions = position_list
                    # Keep first position for compatibility
                    self.current_position = position_list[0]
                    # Debug: log position data
                    # self.logger.debug(f"Position data: {self.current_position}")
                else:
                    # self.logger.debug("trade-event message contains no position data")
                    pass

                # Handle account/collateral updates (for balance tracking)
                # Process this after position updates to ensure current_position is set
                # Enable balance tracking if BALANCE_RECOVERY_ENABLED or ASSET_LOSSCUT/TAKEPROFIT is configured
                collateral_list = data.get("collateral", [])
                needs_balance_tracking = BALANCE_RECOVERY_ENABLED or ASSET_LOSSCUT_PERCENTAGE > 0 or ASSET_TAKEPROFIT_PERCENTAGE > 0
                if collateral_list and needs_balance_tracking:
                    for collateral_data in collateral_list:
                        # Extract available balance
                        amount = collateral_data.get("amount")
                        if amount is not None:
                            # EdgeX account.amount behavior:
                            # - No position: account.amount = actual total equity
                            # - LONG position: account.amount = actual equity - position value
                            # - SHORT position: account.amount = actual equity + position value
                            # We need to normalize this to get the actual total equity
                            available_balance = float(amount)

                            # Calculate position adjustment if we have a position
                            position_adjustment = 0.0
                            if self.current_position is not None:
                                open_size_str = self.current_position.get("openSize")
                                open_value_str = self.current_position.get("openValue")

                                if open_size_str is not None and open_value_str is not None:
                                    size = float(open_size_str)
                                    open_value = float(open_value_str)
                                    position_value = abs(open_value)

                                    # Only adjust if position exists
                                    if abs(size) >= 0.0001:
                                        if size > 0:  # LONG
                                            # LONG: account.amount is reduced by position value, so add it back
                                            position_adjustment = position_value
                                        elif size < 0:  # SHORT
                                            # SHORT: account.amount is increased by position value, so subtract it back
                                            position_adjustment = -position_value

                            # Calculate actual total equity
                            self.current_balance = available_balance + position_adjustment
                            self.logger.debug(f"Balance updated: available={available_balance:.2f}, adjustment={position_adjustment:+.2f}, total={self.current_balance:.2f} USD")

                # Calculate unrealized PnL if we have both position and price
                if position_list:
                    self._calculate_and_log_unrealized_pnl()

            except Exception as e:
                self.logger.error(f"Error handling trade-event message: {str(e)}")

        # Register handler for quote-event messages (contains ticker updates)
        def quote_event_handler(message: str) -> None:
            try:
                msg = json.loads(message)
                # Handle ticker updates from quote-event
                if msg.get("type") == "quote-event":
                    channel = msg.get("channel", "")
                    if "ticker" in channel.lower():
                        # EdgeX ticker structure: quote-event -> content -> data (array) -> data[0]
                        content = msg.get("content", {})
                        data_list = content.get("data", [])

                        if not data_list:
                            self.logger.debug("No ticker data in quote-event")
                            return

                        data = data_list[0]
                        last_price = data.get("lastPrice") or data.get("last_price")
                        if last_price is not None:
                            # self.logger.debug(f"Ticker update: price={last_price}")
                            self.current_price = float(last_price)
                            self._calculate_and_log_unrealized_pnl()
                        else:
                            self.logger.debug(f"Ticker data has no price: {data}")
            except Exception as e:
                self.logger.error(f"Error handling quote-event message: {str(e)}")

        # Register handlers
        self.on_message("trade-event", trade_event_handler)
        self.on_message("ticker", quote_event_handler)
