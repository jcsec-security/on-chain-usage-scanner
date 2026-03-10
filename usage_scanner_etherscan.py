#!/usr/bin/env python3
"""
Scan recent direct + internal calls to a target contract function.

Requirements:
    pip install requests eth-utils

Usage:
    python usage_scanner.py \
        --address 0xYourContract \
        --signature "finalizeEthWithdrawal(uint256,uint256,uint16,bytes,bytes32[])" \
        --days 30 \
        --apikey YOUR_ETHERSCAN_API_KEY \
        --rpc-url https://your-rpc.example

Optional:
    --chainid 1
    --full-address
    --timeout 30
    --trace-timeout 30
    --verbose-trace-errors

What it does:
- Verifies the target address has code
- Pulls recent normal txs from Etherscan V2
- Filters direct calls by 4-byte selector
- Pulls recent internal-tx candidates from Etherscan V2
- Traces each candidate tx through RPC to find internal calls to the target function
- Aggregates unique counterparties and prints:
    [EOA] 0x1234....ff (n txs)

Counterparty attribution:
- direct call: tx.from
- internal call: immediate caller frame.from
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Set, Tuple

import requests
from eth_utils import keccak, to_checksum_address


ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"
EMPTY_CODES = {"0x", "0x0", "0x00", ""}
PARITY_CALL_TYPES = {"call", "staticcall", "delegatecall", "callcode"}
GETH_CALL_TYPES = {"CALL", "STATICCALL", "DELEGATECALL", "CALLCODE"}


class EtherscanError(RuntimeError):
    pass


class RpcTraceError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="List unique counterparties that directly or internally called a target function over the last N days."
    )
    p.add_argument("--address", required=True, help="Target contract address")
    p.add_argument("--signature", required=True, help='Canonical signature, e.g. "transfer(address,uint256)"')
    p.add_argument("--days", required=True, type=int, help="Lookback window in days")
    p.add_argument("--apikey", required=True, help="Etherscan API key")
    p.add_argument("--rpc-url", required=True, help="Tracing-enabled JSON-RPC endpoint")
    p.add_argument("--chainid", default="1", help="Etherscan V2 chainid, default: 1")
    p.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    p.add_argument("--trace-timeout", type=int, default=30, help="Tracer timeout seconds")
    p.add_argument("--full-address", action="store_true", help="Print full addresses instead of shortened form")
    p.add_argument(
        "--verbose-trace-errors",
        action="store_true",
        help="Print per-tx trace errors to stderr",
    )
    return p


def is_hex_address(addr: str) -> bool:
    return isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42


def normalize_hex_address(addr: str) -> str:
    return to_checksum_address(addr)


def function_selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


def fmt_address(addr: str, full: bool) -> str:
    if full:
        return addr
    return f"{addr[:6]}....{addr[-2:]}"


def etherscan_get(session: requests.Session, params: Dict[str, str], timeout: int) -> dict:
    resp = session.get(ETHERSCAN_V2_BASE, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    if "jsonrpc" in data:
        if "result" not in data:
            raise EtherscanError(f"Malformed proxy response: {data}")
        return data

    status = data.get("status")
    result = data.get("result")
    message = data.get("message", "")

    if status == "1":
        return data

    if isinstance(result, str) and result.lower() == "no transactions found":
        return data

    raise EtherscanError(f"Etherscan error: message={message!r}, result={result!r}")


def eth_get_code(
    session: requests.Session,
    address: str,
    apikey: str,
    chainid: str,
    timeout: int,
) -> str:
    data = etherscan_get(
        session,
        {
            "chainid": chainid,
            "module": "proxy",
            "action": "eth_getCode",
            "address": address,
            "tag": "latest",
            "apikey": apikey,
        },
        timeout,
    )
    return data["result"]


def classify_code(code: str) -> str:
    code_lc = (code or "").lower()
    if code_lc in EMPTY_CODES:
        return "[EOA]"
    if code_lc.startswith("0xef0100"):
        return "[7702del]"
    return "[Contract]"


def assert_target_has_code(
    session: requests.Session,
    address: str,
    apikey: str,
    chainid: str,
    timeout: int,
) -> None:
    code = eth_get_code(session, address, apikey, chainid, timeout)
    label = classify_code(code)
    if label == "[EOA]":
        raise SystemExit(f"Provided address is not a contract-like account: {address}")


def fetch_recent_normal_txs(
    session: requests.Session,
    address: str,
    apikey: str,
    chainid: str,
    cutoff_ts: int,
    timeout: int,
) -> List[dict]:
    page = 1
    offset = 1000
    out: List[dict] = []

    while True:
        data = etherscan_get(
            session,
            {
                "chainid": chainid,
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": "0",
                "endblock": "99999999",
                "page": str(page),
                "offset": str(offset),
                "sort": "desc",
                "apikey": apikey,
            },
            timeout,
        )
        result = data.get("result", [])
        if isinstance(result, str) or not result:
            break

        reached_cutoff = False
        for tx in result:
            try:
                ts = int(tx["timeStamp"])
            except Exception:
                continue

            if ts < cutoff_ts:
                reached_cutoff = True
                continue

            out.append(tx)

        if reached_cutoff or len(result) < offset:
            break

        page += 1
        time.sleep(0.2)

    return out


def fetch_recent_internal_candidates(
    session: requests.Session,
    address: str,
    apikey: str,
    chainid: str,
    cutoff_ts: int,
    timeout: int,
) -> List[dict]:
    page = 1
    offset = 1000
    out: List[dict] = []

    while True:
        data = etherscan_get(
            session,
            {
                "chainid": chainid,
                "module": "account",
                "action": "txlistinternal",
                "address": address,
                "startblock": "0",
                "endblock": "99999999",
                "page": str(page),
                "offset": str(offset),
                "sort": "desc",
                "apikey": apikey,
            },
            timeout,
        )
        result = data.get("result", [])
        if isinstance(result, str) or not result:
            break

        reached_cutoff = False
        for itx in result:
            try:
                ts = int(itx["timeStamp"])
            except Exception:
                continue

            if ts < cutoff_ts:
                reached_cutoff = True
                continue

            out.append(itx)

        if reached_cutoff or len(result) < offset:
            break

        page += 1
        time.sleep(0.2)

    return out


def add_counterparty(counterparty_to_txs: Dict[str, Set[str]], counterparty: str, txhash: str) -> None:
    if not is_hex_address(counterparty):
        return
    counterparty_to_txs[normalize_hex_address(counterparty)].add(txhash.lower())


def scan_direct_calls(
    txs: Iterable[dict],
    target_contract: str,
    selector: str,
) -> Dict[str, Set[str]]:
    target_lc = target_contract.lower()
    selector_lc = selector.lower()
    hits: Dict[str, Set[str]] = defaultdict(set)

    for tx in txs:
        to_addr = (tx.get("to") or "").lower()
        inp = (tx.get("input") or "").lower()
        txhash = (tx.get("hash") or "").lower()
        sender = tx.get("from") or ""

        if to_addr != target_lc:
            continue
        if not inp.startswith(selector_lc):
            continue
        if not txhash:
            continue

        add_counterparty(hits, sender, txhash)

    return hits


def rpc_post(session: requests.Session, rpc_url: str, method: str, params: list, timeout: int) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    resp = session.post(rpc_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RpcTraceError(f"{method} failed: {data['error']}")
    return data.get("result")


def trace_tx_parity_style(
    session: requests.Session,
    rpc_url: str,
    txhash: str,
    http_timeout: int,
) -> list:
    result = rpc_post(session, rpc_url, "trace_transaction", [txhash], timeout=http_timeout)
    if not isinstance(result, list):
        raise RpcTraceError("trace_transaction returned unexpected payload")
    return result


def trace_tx_geth_call_tracer(
    session: requests.Session,
    rpc_url: str,
    txhash: str,
    trace_timeout: int,
    http_timeout: int,
) -> dict:
    params = [
        txhash,
        {
            "tracer": "callTracer",
            "timeout": f"{trace_timeout}s",
        },
    ]
    result = rpc_post(session, rpc_url, "debug_traceTransaction", params, timeout=http_timeout)
    if not isinstance(result, dict):
        raise RpcTraceError("debug_traceTransaction returned unexpected payload")
    return result


def walk_call_tracer_tree(
    node: dict,
    target_contract: str,
    selector: str,
    txhash: str,
    depth: int,
    out: Dict[str, Set[str]],
) -> None:
    """
    depth >= 1 means internal frame.
    depth == 0 is the top-level tx call and is intentionally skipped here
    to avoid double-counting with the direct tx scan.
    """
    to_addr = (node.get("to") or "").lower()
    frm = node.get("from") or ""
    inp = (node.get("input") or "").lower()
    typ = (node.get("type") or "").upper()

    if depth >= 1 and typ in GETH_CALL_TYPES and to_addr == target_contract.lower() and inp.startswith(selector.lower()):
        add_counterparty(out, frm, txhash)

    for child in node.get("calls", []) or []:
        if isinstance(child, dict):
            walk_call_tracer_tree(child, target_contract, selector, txhash, depth + 1, out)


def scan_internal_calls_via_tracing(
    session: requests.Session,
    rpc_url: str,
    candidate_txhashes: Iterable[str],
    target_contract: str,
    selector: str,
    trace_timeout: int,
    http_timeout: int,
    verbose_trace_errors: bool,
) -> Tuple[Dict[str, Set[str]], Dict[str, str]]:
    """
    Returns:
      - hits: counterparty -> unique tx hashes
      - trace_backend_by_tx: txhash -> backend used ("parity", "geth", "failed")
    """
    hits: Dict[str, Set[str]] = defaultdict(set)
    backend_used: Dict[str, str] = {}

    for txhash in sorted({h.lower() for h in candidate_txhashes if h}):
        # Prefer parity/erigon style first.
        try:
            traces = trace_tx_parity_style(
                session=session,
                rpc_url=rpc_url,
                txhash=txhash,
                http_timeout=http_timeout,
            )

            for tr in traces:
                if not isinstance(tr, dict):
                    continue

                trace_address = tr.get("traceAddress", [])
                if not isinstance(trace_address, list) or len(trace_address) == 0:
                    # top-level frame, skip to avoid double-counting direct calls
                    continue

                typ = (tr.get("type") or "").lower()
                action = tr.get("action") or {}
                to_addr = (action.get("to") or "").lower()
                frm = action.get("from") or ""
                inp = (action.get("input") or "").lower()

                if typ in PARITY_CALL_TYPES and to_addr == target_contract.lower() and inp.startswith(selector.lower()):
                    add_counterparty(hits, frm, txhash)

            backend_used[txhash] = "parity"
            time.sleep(0.05)
            continue

        except Exception as e:
            if verbose_trace_errors:
                print(f"[trace_transaction failed] {txhash}: {e}", file=sys.stderr)

        # Fallback to geth debug tracer.
        try:
            root = trace_tx_geth_call_tracer(
                session=session,
                rpc_url=rpc_url,
                txhash=txhash,
                trace_timeout=trace_timeout,
                http_timeout=http_timeout,
            )
            walk_call_tracer_tree(
                node=root,
                target_contract=target_contract,
                selector=selector,
                txhash=txhash,
                depth=0,
                out=hits,
            )
            backend_used[txhash] = "geth"
            time.sleep(0.05)
            continue

        except Exception as e:
            if verbose_trace_errors:
                print(f"[debug_traceTransaction failed] {txhash}: {e}", file=sys.stderr)
            backend_used[txhash] = "failed"
            time.sleep(0.05)

    return hits, backend_used


def merge_counterparty_maps(*maps: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = defaultdict(set)
    for mp in maps:
        for addr, txs in mp.items():
            out[addr].update(txs)
    return out


def classify_addresses(
    session: requests.Session,
    addresses: Iterable[str],
    apikey: str,
    chainid: str,
    timeout: int,
) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for addr in sorted(set(addresses), key=str.lower):
        code = eth_get_code(session, addr, apikey, chainid, timeout)
        out[addr] = classify_code(code)
        time.sleep(0.1)
    return out


def main() -> int:
    args = build_parser().parse_args()

    if args.days < 0:
        raise SystemExit("--days must be >= 0")
    if not is_hex_address(args.address):
        raise SystemExit(f"Invalid target address: {args.address}")

    target_contract = normalize_hex_address(args.address)
    selector = function_selector(args.signature)
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
    cutoff_ts = int(cutoff_dt.timestamp())

    session = requests.Session()
    session.headers.update({"User-Agent": "usage-scanner/1.1"})

    assert_target_has_code(
        session=session,
        address=target_contract,
        apikey=args.apikey,
        chainid=args.chainid,
        timeout=args.timeout,
    )

    normal_txs = fetch_recent_normal_txs(
        session=session,
        address=target_contract,
        apikey=args.apikey,
        chainid=args.chainid,
        cutoff_ts=cutoff_ts,
        timeout=args.timeout,
    )
    direct_hits = scan_direct_calls(
        txs=normal_txs,
        target_contract=target_contract,
        selector=selector,
    )

    internal_candidates = fetch_recent_internal_candidates(
        session=session,
        address=target_contract,
        apikey=args.apikey,
        chainid=args.chainid,
        cutoff_ts=cutoff_ts,
        timeout=args.timeout,
    )
    candidate_txhashes = {str(x.get("hash", "")).lower() for x in internal_candidates if x.get("hash")}

    internal_hits, backend_by_tx = scan_internal_calls_via_tracing(
        session=session,
        rpc_url=args.rpc_url,
        candidate_txhashes=candidate_txhashes,
        target_contract=target_contract,
        selector=selector,
        trace_timeout=args.trace_timeout,
        http_timeout=args.timeout,
        verbose_trace_errors=args.verbose_trace_errors,
    )

    merged = merge_counterparty_maps(direct_hits, internal_hits)
    labels = classify_addresses(
        session=session,
        addresses=merged.keys(),
        apikey=args.apikey,
        chainid=args.chainid,
        timeout=args.timeout,
    )

    direct_match_count = sum(len(v) for v in direct_hits.values())
    internal_match_count = sum(len(v) for v in internal_hits.values())
    total_unique_counterparties = len(merged)

    traced_parity = sum(1 for v in backend_by_tx.values() if v == "parity")
    traced_geth = sum(1 for v in backend_by_tx.values() if v == "geth")
    traced_failed = sum(1 for v in backend_by_tx.values() if v == "failed")

    print(f"# Contract: {target_contract}")
    print(f"# Function signature: {args.signature}")
    print(f"# Selector: {selector}")
    print(f"# Lookback days: {args.days}")
    print(f"# Cutoff UTC: {cutoff_dt.isoformat()}")
    print(f"# Normal txs scanned: {len(normal_txs)}")
    print(f"# Internal candidate tx hashes: {len(candidate_txhashes)}")
    print(f"# Candidate tx hashes traced via trace_transaction: {traced_parity}")
    print(f"# Candidate tx hashes traced via debug_traceTransaction: {traced_geth}")
    print(f"# Candidate tx hashes trace-failed: {traced_failed}")
    print(f"# Direct matched txs: {direct_match_count}")
    print(f"# Internal matched txs: {internal_match_count}")
    print(f"# Unique counterparties: {total_unique_counterparties}")
    print()

    rows = []
    for addr, txhashes in merged.items():
        label = labels.get(addr, "[EOA]")
        rows.append((label, addr, len(txhashes)))

    rows.sort(key=lambda x: (-x[2], x[1].lower()))

    for label, addr, n in rows:
        print(f"{label} {fmt_address(addr, args.full_address)} ({n} txs)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
