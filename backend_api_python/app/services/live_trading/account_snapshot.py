"""
Live account snapshot for a saved credential: swap/spot positions + open orders.
Used by broker-accounts UI (not strategy L3 ledger).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from app.services.exchange_execution import resolve_exchange_config
from app.services.live_trading.factory import create_client
from app.services.live_trading.records import normalize_strategy_symbol
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _user_facing_exchange_error(exc: Exception, *, context: str) -> str:
    """Map raw exchange exception to a short UI message."""
    msg = str(exc or "")
    low = msg.lower()
    if "451" in msg or "restricted location" in low or "service unavailable from a restricted location" in low:
        return (
            "Binance 地区限制 (HTTP 451)：当前 backend 服务器/IP 所在地区不在 Binance 服务范围内。"
            "请将 Docker 部署到 Binance 允许的地区，或为 backend 配置 HTTP 代理后重试；"
            "也可改用 OKX / Bitget 等交易所。"
        )
    if "50119" in msg or "api key doesn't exist" in low:
        return f"{context}：API Key 无效或已在 OKX 删除，请到个人中心重新绑定凭证"
    if "50111" in msg or "invalid ok-access-key" in low:
        return f"{context}：API Key 无效，请检查凭证是否正确"
    if "50113" in msg or "invalid sign" in low:
        return f"{context}：签名错误，请检查 Secret Key 与 Passphrase"
    if "401" in msg or "403" in msg:
        return f"{context}：交易所鉴权失败，请更新 API Key / Secret / Passphrase"
    if "418" in msg or "rate limit" in low or "too many requests" in low:
        return f"{context}：请求过于频繁，请稍后再试"
    short = msg.replace("\n", " ").strip()
    if len(short) > 160:
        short = short[:160] + "…"
    return f"{context}：{short}" if short else f"{context}：拉取失败"


def _error_fingerprint(line: str) -> str:
    """Group equivalent exchange errors (e.g. same 451 on swap + spot)."""
    low = str(line or "").lower()
    if "451" in line or "restricted location" in low:
        return "geo:451"
    if "50119" in line or "api key doesn't exist" in low:
        return "auth:50119"
    if "50111" in line or "invalid ok-access-key" in low:
        return "auth:50111"
    if "401" in line or "403" in line or "鉴权失败" in line:
        return "auth:http"
    if "418" in line or "rate limit" in low:
        return "rate:limit"
    return line.strip()


def _append_snapshot_error(errors: List[str], exc: Exception, *, context: str) -> None:
    line = _user_facing_exchange_error(exc, context=context)
    if line not in errors:
        errors.append(line)
    logger.warning("%s: %s", context, exc)


def _parse_okx_positions(data: List[Dict[str, Any]], *, market_type: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in data or []:
        if not isinstance(p, dict):
            continue
        inst_id = str(p.get("instId") or "")
        pos_side = str(p.get("posSide") or "").lower()
        try:
            pos = float(p.get("pos") or 0.0)
        except Exception:
            pos = 0.0
        if not inst_id or abs(pos) <= 0:
            continue
        hb_sym = inst_id.replace("-SWAP", "").replace("-", "/")
        if pos_side == "long":
            side = "long"
        elif pos_side == "short":
            side = "short"
        elif pos_side == "net":
            side = "long" if pos > 0 else "short"
        else:
            side = "long" if pos > 0 else "short"
        try:
            entry = float(p.get("avgPx") or 0.0)
        except Exception:
            entry = 0.0
        out.append(
            {
                "symbol": normalize_strategy_symbol(hb_sym) or hb_sym,
                "side": side,
                "size": abs(float(pos)),
                "entry_price": entry,
                "market_type": market_type,
                "inst_id": inst_id,
            }
        )
    return out


def _parse_binance_futures_positions(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in rows or []:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or "").strip().upper()
        try:
            amt = float(p.get("positionAmt") or 0.0)
            ep = float(p.get("entryPrice") or 0.0)
        except Exception:
            amt = 0.0
            ep = 0.0
        if not sym or abs(amt) <= 0:
            continue
        hb_sym = sym
        if hb_sym.endswith("USDT") and len(hb_sym) > 4 and "/" not in hb_sym:
            hb_sym = f"{hb_sym[:-4]}/USDT"
        side = "long" if amt > 0 else "short"
        out.append(
            {
                "symbol": normalize_strategy_symbol(hb_sym) or hb_sym,
                "side": side,
                "size": abs(float(amt)),
                "entry_price": ep,
                "market_type": "swap",
                "inst_id": sym,
            }
        )
    return out


def _parse_okx_orders(data: List[Dict[str, Any]], *, market_type: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for o in data or []:
        if not isinstance(o, dict):
            continue
        inst_id = str(o.get("instId") or "")
        if not inst_id:
            continue
        sym = inst_id.replace("-SWAP", "").replace("-", "/")
        try:
            px = float(o.get("px") or 0.0)
            sz = float(o.get("sz") or 0.0)
            filled = float(o.get("accFillSz") or 0.0)
        except Exception:
            px, sz, filled = 0.0, 0.0, 0.0
        side_raw = str(o.get("side") or "").lower()
        side = "buy" if side_raw == "buy" else "sell" if side_raw == "sell" else side_raw
        out.append(
            {
                "symbol": normalize_strategy_symbol(sym) or sym,
                "side": side,
                "market_type": market_type,
                "order_type": str(o.get("ordType") or ""),
                "price": px,
                "amount": sz,
                "filled": filled,
                "exchange_order_id": str(o.get("ordId") or ""),
                "status": str(o.get("state") or "live"),
                "inst_id": inst_id,
            }
        )
    return out


def _parse_binance_orders(rows: List[Dict[str, Any]], *, market_type: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for o in rows or []:
        if not isinstance(o, dict):
            continue
        sym = str(o.get("symbol") or "")
        if not sym:
            continue
        hb = sym
        if hb.endswith("USDT") and "/" not in hb and len(hb) > 4:
            hb = f"{hb[:-4]}/USDT"
        try:
            px = float(o.get("price") or 0.0)
            sz = float(o.get("origQty") or 0.0)
            filled = float(o.get("executedQty") or 0.0)
        except Exception:
            px, sz, filled = 0.0, 0.0, 0.0
        side = str(o.get("side") or "").lower()
        out.append(
            {
                "symbol": normalize_strategy_symbol(hb) or hb,
                "side": side,
                "market_type": market_type,
                "order_type": str(o.get("type") or ""),
                "price": px,
                "amount": sz,
                "filled": filled,
                "exchange_order_id": str(o.get("orderId") or ""),
                "status": str(o.get("status") or "NEW"),
                "inst_id": sym,
            }
        )
    return out


def _fetch_okx_snapshot(
    client, exchange_id: str, errors: List[str]
) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    from app.services.live_trading.okx import OkxClient

    if not isinstance(client, OkxClient):
        return [], [], []
    swap_pos: List[Dict[str, Any]] = []
    spot_pos: List[Dict[str, Any]] = []
    orders: List[Dict[str, Any]] = []
    try:
        resp = client.get_positions(inst_type="SWAP")
        data = (resp.get("data") or []) if isinstance(resp, dict) else []
        swap_pos = _parse_okx_positions(data, market_type="swap")
    except Exception as e:
        _append_snapshot_error(errors, e, context="OKX 合约持仓")
    try:
        resp = client.get_positions(inst_type="SPOT")
        data = (resp.get("data") or []) if isinstance(resp, dict) else []
        spot_pos = _parse_okx_positions(data, market_type="spot")
    except Exception as e:
        logger.debug("OKX spot positions snapshot skipped: %s", e)
    for inst_type, mt, label in (
        ("SWAP", "swap", "OKX 合约挂单"),
        ("SPOT", "spot", "OKX 现货挂单"),
    ):
        try:
            resp = client._signed_request(
                "GET", "/api/v5/trade/orders-pending", params={"instType": inst_type}
            )
            data = (resp.get("data") or []) if isinstance(resp, dict) else []
            orders.extend(_parse_okx_orders(data, market_type=mt))
        except Exception as e:
            _append_snapshot_error(errors, e, context=label)
    return swap_pos, spot_pos, orders


def _as_list_payload(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        raw = data.get("raw")
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        inner = data.get("data")
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
    return []


def _fetch_binance_snapshot(
    client, market_type: str, errors: List[str]
) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    from app.services.live_trading.binance import BinanceFuturesClient
    from app.services.live_trading.binance_spot import BinanceSpotClient

    swap_pos: List[Dict[str, Any]] = []
    spot_pos: List[Dict[str, Any]] = []
    orders: List[Dict[str, Any]] = []
    if isinstance(client, BinanceFuturesClient):
        try:
            rows = client.get_positions() or []
            if isinstance(rows, dict) and "raw" in rows:
                rows = rows["raw"]
            swap_pos = _parse_binance_futures_positions(rows if isinstance(rows, list) else [])
        except Exception as e:
            _append_snapshot_error(errors, e, context="Binance 合约持仓")
        try:
            rows = client._signed_request("GET", "/fapi/v1/openOrders", params={})
            orders = _parse_binance_orders(_as_list_payload(rows), market_type="swap")
        except Exception as e:
            _append_snapshot_error(errors, e, context="Binance 合约挂单")
    elif isinstance(client, BinanceSpotClient):
        try:
            rows = client._signed_request("GET", "/api/v3/openOrders", params={})
            orders = _parse_binance_orders(_as_list_payload(rows), market_type="spot")
        except Exception as e:
            _append_snapshot_error(errors, e, context="Binance 现货挂单")
    return swap_pos, spot_pos, orders


def fetch_account_snapshot(*, user_id: int, credential_id: int) -> Dict[str, Any]:
    """Live fetch swap/spot legs + open orders for one credential."""
    cred = int(credential_id or 0)
    errors: List[str] = []
    if cred <= 0:
        return {
            "swap_positions": [],
            "spot_positions": [],
            "open_orders": [],
            "fetched_at": int(time.time()),
            "error": "missing_credential_id",
            "warnings": ["缺少 credential_id"],
        }
    exchange_config = resolve_exchange_config({"credential_id": cred}, user_id=int(user_id))
    exchange_id = str(exchange_config.get("exchange_id") or "").strip().lower()
    if not exchange_id:
        return {
            "swap_positions": [],
            "spot_positions": [],
            "open_orders": [],
            "fetched_at": int(time.time()),
            "error": "missing_exchange_id",
            "warnings": ["凭证未配置 exchange_id"],
        }

    swap_all: List[Dict[str, Any]] = []
    spot_all: List[Dict[str, Any]] = []
    orders_all: List[Dict[str, Any]] = []

    if exchange_id in ("okx", "okex"):
        try:
            client = create_client(exchange_config, market_type="swap")
            sp, st, od = _fetch_okx_snapshot(client, exchange_id, errors)
            swap_all.extend(sp)
            spot_all.extend(st)
            orders_all.extend(od)
        except Exception as e:
            _append_snapshot_error(errors, e, context="OKX 账户连接")
    elif exchange_id in ("binance", "binanceusdm", "binancefutures"):
        for market_type in ("swap", "spot"):
            try:
                client = create_client(exchange_config, market_type=market_type)
            except Exception as e:
                _append_snapshot_error(errors, e, context=f"Binance {market_type} 连接")
                continue
            sp, st, od = _fetch_binance_snapshot(client, market_type, errors)
            swap_all.extend(sp)
            spot_all.extend(st)
            orders_all.extend(od)
    else:
        try:
            client = create_client(exchange_config, market_type="swap")
            from app.services.live_trading.binance import BinanceFuturesClient

            if isinstance(client, BinanceFuturesClient):
                sp, _, od = _fetch_binance_snapshot(client, "swap", errors)
                swap_all.extend(sp)
                orders_all.extend(od)
            elif hasattr(client, "get_positions"):
                resp = client.get_positions() or {}
                data = resp.get("data") if isinstance(resp, dict) else resp
                if isinstance(data, list):
                    swap_all.extend(_parse_okx_positions(data, market_type="swap"))
            else:
                errors.append(f"{exchange_id}：暂不支持账户快照，请使用 OKX / Binance")
        except Exception as e:
            _append_snapshot_error(errors, e, context=f"{exchange_id} 账户连接")

    # De-dupe orders by exchange_order_id
    seen: set = set()
    deduped_orders: List[Dict[str, Any]] = []
    for o in orders_all:
        oid = str(o.get("exchange_order_id") or "")
        key = oid or f"{o.get('symbol')}-{o.get('side')}-{o.get('price')}"
        if key in seen:
            continue
        seen.add(key)
        deduped_orders.append(o)

    has_data = bool(swap_all or spot_all or deduped_orders)
    uniq_errors: List[str] = []
    seen_fp: set = set()
    for e in errors:
        fp = _error_fingerprint(e)
        if fp in seen_fp:
            continue
        seen_fp.add(fp)
        uniq_errors.append(e)

    out: Dict[str, Any] = {
        "swap_positions": swap_all,
        "spot_positions": spot_all,
        "open_orders": deduped_orders,
        "fetched_at": int(time.time()),
        "exchange_id": exchange_id,
        "warnings": uniq_errors,
        "partial": bool(uniq_errors) and has_data,
    }
    if uniq_errors and not has_data:
        out["error"] = uniq_errors[0]
    elif uniq_errors:
        out["error"] = ""
    return out
