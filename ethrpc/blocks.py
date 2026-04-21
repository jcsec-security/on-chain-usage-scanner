"""Block-number helpers: latest, by-number, window resolution with binary-search refinement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from .client import RpcError, hex_to_int, int_to_hex, rpc_post


def latest_block(session: requests.Session, rpc_url: str, timeout: int = 15) -> int:
    result = rpc_post(session, rpc_url, "eth_blockNumber", [], timeout)
    if not isinstance(result, str):
        raise RpcError("eth_blockNumber returned unexpected payload")
    return hex_to_int(result)


def get_block_by_number(
    session: requests.Session, rpc_url: str, block: int, timeout: int = 15,
) -> dict:
    result = rpc_post(
        session, rpc_url, "eth_getBlockByNumber",
        [int_to_hex(block), False], timeout,
    )
    if not isinstance(result, dict):
        raise RpcError("eth_getBlockByNumber returned unexpected payload")
    return result


def estimate_start_block_by_avg_time(latest: int, days: int, avg_block_time: int) -> int:
    blocks_back = int((days * 86400) / max(avg_block_time, 1))
    return max(0, latest - blocks_back)


def refine_start_block_by_timestamp(
    session: requests.Session,
    rpc_url: str,
    latest: int,
    target_ts: int,
    rough_start: int,
    timeout: int,
) -> int:
    """Binary search for the first block whose timestamp >= target_ts."""
    lo = max(0, rough_start)
    hi = latest

    blk = get_block_by_number(session, rpc_url, lo, timeout)
    if hex_to_int(blk["timestamp"]) > target_ts:
        lo = 0  # rough estimate overshot the cutoff; widen search

    while lo < hi:
        mid = (lo + hi) // 2
        blk = get_block_by_number(session, rpc_url, mid, timeout)
        if hex_to_int(blk["timestamp"]) < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def resolve_window(
    session: requests.Session,
    rpc_url: str,
    window_days: int,
    avg_block_time: int = 12,
    timeout: int = 15,
) -> tuple[int, int]:
    """Returns (start_block, end_block) covering the last `window_days`."""
    latest = latest_block(session, rpc_url, timeout)
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=window_days)).timestamp())
    rough = estimate_start_block_by_avg_time(latest, window_days, avg_block_time)
    start = refine_start_block_by_timestamp(session, rpc_url, latest, cutoff_ts, rough, timeout)
    return start, latest
