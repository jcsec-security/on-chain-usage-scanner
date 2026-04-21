"""Account/code classification: [EOA] / [Contract] / [7702del]."""

from __future__ import annotations

from typing import Iterable

import requests

from .client import RpcError, rpc_batch, rpc_post


EMPTY_CODES = {"0x", "0x0", "0x00", ""}


def classify_code(code: str) -> str:
    """
    Classify a hex-string result from eth_getCode:
      "[EOA]"      empty code
      "[7702del]"  EIP-7702 delegation marker (starts with 0xef0100)
      "[Contract]" any other bytecode

    Reflects the current "latest" snapshot only. An account that's a
    contract today may have been an EOA earlier.
    """
    code_lc = (code or "").lower()
    if code_lc in EMPTY_CODES:
        return "[EOA]"
    if code_lc.startswith("0xef0100"):
        return "[7702del]"
    return "[Contract]"


def eth_get_code(
    session: requests.Session, rpc_url: str, address: str, timeout: int,
) -> str:
    result = rpc_post(session, rpc_url, "eth_getCode", [address, "latest"], timeout)
    if not isinstance(result, str):
        raise RpcError("eth_getCode returned unexpected payload")
    return result


def classify_addresses_batch(
    session: requests.Session,
    rpc_url: str,
    addresses: Iterable[str],
    timeout: int,
) -> dict[str, str]:
    """Batch-classify every address in one JSON-RPC request."""
    addr_list = sorted(set(addresses), key=str.lower)
    if not addr_list:
        return {}
    calls = [("eth_getCode", [a, "latest"]) for a in addr_list]
    results = rpc_batch(session, rpc_url, calls, timeout)
    return {a: classify_code(c or "0x") for a, c in zip(addr_list, results)}
