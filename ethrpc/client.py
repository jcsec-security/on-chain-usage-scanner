"""Low-level JSON-RPC client for Ethereum. Session-managed, single + batch calls."""

from __future__ import annotations

from typing import Any

import requests


class RpcError(RuntimeError):
    """Raised for JSON-RPC protocol and transport-level failures."""


def make_session(user_agent: str = "ethrpc/1.0") -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent, "Content-Type": "application/json"})
    return s


def hex_to_int(x: str) -> int:
    return int(x, 16)


def int_to_hex(x: int) -> str:
    return hex(x)


def rpc_post(
    session: requests.Session,
    rpc_url: str,
    method: str,
    params: list,
    timeout: int,
) -> Any:
    """Single JSON-RPC call. Raises RpcError on protocol error."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = session.post(rpc_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RpcError(f"{method} failed: {data['error']}")
    return data.get("result")


def rpc_batch(
    session: requests.Session,
    rpc_url: str,
    calls: list[tuple[str, list]],
    timeout: int,
) -> list[Any]:
    """
    JSON-RPC batch. `calls` is a list of (method, params) tuples.
    Returns results in the same order as `calls`; None for any call that errored.
    """
    payload = [
        {"jsonrpc": "2.0", "id": i, "method": m, "params": p}
        for i, (m, p) in enumerate(calls)
    ]
    resp = session.post(rpc_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    items = resp.json()
    ordered: list[Any] = [None] * len(calls)
    for item in items:
        idx = item.get("id")
        if idx is not None and 0 <= idx < len(ordered):
            ordered[idx] = item.get("result")
    return ordered
