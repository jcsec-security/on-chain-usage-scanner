"""
Account/code classification: EOA vs Contract vs EIP-7702 delegation.

NOTE ON EIP-7702:
  EIP-7702 (active on mainnet) lets an EOA temporarily delegate its code to
  a contract. These accounts store a magic prefix `0xef0100` followed by the
  implementation address. We classify them separately as `[7702del]` so
  callers can distinguish them from "true" contracts with arbitrary runtime
  bytecode.

LIMITATION: An address flagged `[Contract]` today may have been an EOA
yesterday (the classification is a `latest` snapshot). If historical
distinction matters, read code at the specific block.
"""

from __future__ import annotations

from typing import Iterable

import requests

from .client import RpcError, rpc_batch, rpc_post


EMPTY_CODES = {"0x", "0x0", "0x00", ""}


def classify_code(code: str) -> str:
    """
    Classify a hex-string result from eth_getCode:
        "[EOA]"       : empty code (plain externally owned account)
        "[7702del]"   : EIP-7702 delegation marker (EOA pointing to a contract)
        "[Contract]"  : any other bytecode

    The input is expected to be the raw hex string from eth_getCode (with or
    without "0x" prefix, case-insensitive).
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
