"""Signal policy helpers for the live trading executor."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from app.services.indicator_params import StrategyConfigParser
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)


class TradingSignalPolicyMixin:
    def _position_state(self, positions: List[Dict[str, Any]]) -> str:
        """
        Return current position state for a strategy+symbol in local single-position mode.

        Returns: 'flat' | 'long' | 'short'
        """
        try:
            if not positions:
                return "flat"
            # Local mode assumes single-direction position per symbol.
            side = (positions[0].get("side") or "").strip().lower()
            if side in ("long", "short"):
                return side
        except Exception:
            pass
        return "flat"

    @staticmethod
    def _symbol_match_key(symbol: str) -> str:
        return str(symbol or "").split(":")[0].strip()

    def _inflight_open_side(self, strategy_id: int, symbol: str) -> Optional[str]:
        """
        Return 'long' or 'short' when an open_* order is pending/processing for
        this strategy+symbol, else None.
        """
        sym_key = self._symbol_match_key(symbol)
        if not sym_key:
            return None
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                cur.execute(
                    """
                    SELECT signal_type, symbol
                    FROM pending_orders
                    WHERE strategy_id = %s
                      AND status IN ('pending', 'processing')
                      AND signal_type IN ('open_long', 'open_short')
                    ORDER BY id DESC
                    LIMIT 20
                    """,
                    (int(strategy_id),),
                )
                rows = cur.fetchall() or []
                cur.close()
            for row in rows:
                row_sym = self._symbol_match_key(str(row.get("symbol") or ""))
                if row_sym != sym_key:
                    continue
                sig = str(row.get("signal_type") or "").strip().lower()
                if sig == "open_long":
                    return "long"
                if sig == "open_short":
                    return "short"
        except Exception as e:
            logger.debug("inflight open lookup failed sid=%s: %s", strategy_id, e)
        return None

    def _effective_position_state(
        self,
        strategy_id: int,
        symbol: str,
        positions: List[Dict[str, Any]],
    ) -> str:
        """Local DB state plus in-flight open orders (live dedup guard)."""
        state = self._position_state(positions)
        if state != "flat":
            return state
        inflight = self._inflight_open_side(strategy_id, symbol)
        return inflight or "flat"

    @staticmethod
    def _is_live_script_hydrate_candidate(trading_config: Optional[Dict[str, Any]]) -> bool:
        tc = trading_config if isinstance(trading_config, dict) else {}
        if str(tc.get("execution_mode") or "live").strip().lower() != "live":
            return False
        bot_type = str(tc.get("bot_type") or "").strip().lower()
        if bot_type == "grid":
            return True
        is_bot_script = bool(
            bot_type in ("martingale", "dca")
            or tc.get("strategy_mode") == "bot"
        )
        return not is_bot_script

    def _is_signal_allowed(
        self,
        state: str,
        signal_type: str,
    ) -> bool:
        """
        Enforce strict state machine:
        - flat: only open_long/open_short
        - long: only add_long/close_long
        - short: only add_short/close_short

        Explicit four-way indicator and script signals may flip by first closing the
        opposing leg, then opening the requested side.
        """
        st = (state or "flat").strip().lower()
        sig = (signal_type or "").strip().lower()
        if st == "flat":
            return sig in ("open_long", "open_short")
        if st == "long":
            return sig in ("add_long", "reduce_long", "close_long")
        if st == "short":
            return sig in ("add_short", "reduce_short", "close_short")
        return False

    def _signal_priority(self, signal_type: str) -> int:
        """
        Lower value = higher priority. We always close before (re)opening/adding.
        """
        sig = (signal_type or "").strip().lower()
        if sig.startswith("close_"):
            return 0
        if sig.startswith("reduce_"):
            return 1
        if sig.startswith("open_"):
            return 2
        if sig.startswith("add_"):
            return 3
        return 99

    def _dedup_key(self, strategy_id: int, symbol: str, signal_type: str, signal_ts: int) -> str:
        sym = (symbol or "").strip().upper()
        if ":" in sym:
            sym = sym.split(":", 1)[0]
        return f"{int(strategy_id)}|{sym}|{(signal_type or '').strip().lower()}|{int(signal_ts or 0)}"

    def _should_skip_signal_once_per_candle(
        self,
        strategy_id: int,
        symbol: str,
        signal_type: str,
        signal_ts: int,
        timeframe_seconds: int,
        now_ts: Optional[int] = None,
    ) -> bool:
        """
        Prevent repeated orders for the same candle signal across ticks.

        This is especially important for 'confirmed' signals that point to the previous closed candle:
        the signal timestamp stays constant for the entire next candle, so without de-dup the system
        would re-enqueue the same order every tick.
        """
        try:
            now = int(now_ts or time.time())
            tf = int(timeframe_seconds or 0)
            if tf <= 0:
                tf = 60
            # Keep keys long enough to cover at least the next candle.
            ttl_sec = max(tf * 2, 120)
            expiry = float(now + ttl_sec)
            key = self._dedup_key(strategy_id, symbol, signal_type, int(signal_ts or 0))

            with self._signal_dedup_lock:
                bucket = self._signal_dedup.get(int(strategy_id))
                if bucket is None:
                    bucket = {}
                    self._signal_dedup[int(strategy_id)] = bucket

                # Opportunistic cleanup
                stale = [k for k, exp in bucket.items() if float(exp) <= now]
                for k in stale[:512]:
                    try:
                        del bucket[k]
                    except Exception:
                        pass

                exp = bucket.get(key)
                if exp is not None and float(exp) > now:
                    return True

                # Reserve the key (best-effort). Caller may still fail to enqueue; that's acceptable
                # because repeated failures should not flood the queue.
                bucket[key] = expiry
                return False
        except Exception:
            return False

    def _to_ratio(self, v: Any, default: float = 0.0) -> float:
        """
        Convert a stored percent value into ratio in [0, 1].

        Convention (single source of truth): ``trading_config.*_pct`` fields
        store percent (e.g. ``9`` means 9%, ``0.01`` means 0.01%). This is
        the same unit the snapshot resolver and the bot wizard already
        produce.

        promoted sub-1% inputs (0.01, 0.5, etc.) to ratio interpretation
        (1%, 50%), which broke strategies needing < 1% SL / TP and was
        the only place left in the system that did that guessing.
        """
        try:
            x = float(v if v is not None else default)
        except Exception:
            x = float(default or 0.0)
        if x < 0:
            return 0.0
        x = x / 100.0
        if x > 1.0:
            return 1.0
        return float(x)

    def _code_strategy_cfg(self, trading_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        tc = trading_config if isinstance(trading_config, dict) else {}
        code_cfg = tc.get("_strategy_cfg_from_code")
        return code_cfg if isinstance(code_cfg, dict) else {}

    def _exit_owner_from_trading_config(self, trading_config: Optional[Dict[str, Any]]) -> str:
        tc = trading_config if isinstance(trading_config, dict) else {}
        code_cfg = self._code_strategy_cfg(tc)
        owner = (
            code_cfg.get("exitOwner")
            or code_cfg.get("exit_owner")
            or tc.get("exit_owner")
            or tc.get("exitOwner")
            or ""
        )
        return str(owner or "").strip().lower()

    def _strategy_owns_exits(self, trading_config: Optional[Dict[str, Any]]) -> bool:
        return self._exit_owner_from_trading_config(trading_config) == "indicator"

    def _risk_params_from_trading_config(self, trading_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Resolve risk/position ratios for live execution.
        Risk controls are code-owned: live execution never reads runtime
        stop/take/trailing fields from the strategy row.
        """
        code_cfg = self._code_strategy_cfg(trading_config)
        if code_cfg and ("risk" in code_cfg or "position" in code_cfg):
            risk = code_cfg.get("risk") or {}
            trailing = risk.get("trailing") or {}
            pos = code_cfg.get("position") or {}
            return {
                "entry_ratio": float(
                    pos.get("entryPct")
                    if pos.get("entryPct") is not None
                    else StrategyConfigParser.normalize_entry_ratio(None)
                ),
                "stop_loss_ratio": float(risk.get("stopLossPct") or 0),
                "take_profit_ratio": float(risk.get("takeProfitPct") or 0),
                "trailing_enabled": bool(trailing.get("enabled")),
                "trailing_stop_ratio": float(trailing.get("pct") or 0),
                "trailing_activation_ratio": float(trailing.get("activationPct") or 0),
            }

        return {
            "entry_ratio": StrategyConfigParser.normalize_entry_ratio(None),
            "stop_loss_ratio": 0.0,
            "take_profit_ratio": 0.0,
            "trailing_enabled": False,
            "trailing_stop_ratio": 0.0,
            "trailing_activation_ratio": 0.0,
        }
