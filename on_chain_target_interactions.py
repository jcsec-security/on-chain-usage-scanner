#!/usr/bin/env python3
"""
RPC-only usage scanner for direct + internal calls to a target function.

Requirements:
    pip install requests eth-utils

Usage:
    python usage_scanner_rpc_only.py \
        --address 0xYourContract \
        --signature "requestL2Transaction(address,uint256,bytes,uint256,uint256,bytes[],address)" \
        --days 14 \
        --rpc-url https://your-tracing-rpc.example

Optional:
    --full-address
    --timeout 30
    --avg-block-time 12
    --chunk-size 50000
    --verbose-trace-errors

What it does:
- Verifies the RPC provider supports trace_filter
- Verifies the target address has code via eth_getCode
- Computes a block range for the last N days
- Uses trace_filter(toAddress=[target]) over that range
- Filters frames by function selector
- Attributes counterparties as:
    - internal call: action.from (when action.from != tx.from)
    - direct top-level call: tx.from via eth_getTransactionByHash
- Classifies counterparties as [EOA] / [Contract] / [7702del]
- Prints:
    [EOA] 0x1234....ff (n txs)

Notes:
- This is designed for Erigon/OpenEthereum/Parity-style trace RPCs.
- It relies on trace_filter support.
- (n txs) counts unique transaction hashes per counterparty.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests
from eth_utils import keccak, to_checksum_address


EMPTY_CODES = {"0x", "0x0", "0x00", ""}


class RpcError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------

class ProgressBar:
    """
    Simple terminal progress bar that prints to stderr so it does not
    interfere with the script's stdout output.

    Usage:
        bar = ProgressBar(total=100, prefix="Scanning")
        bar.update(10)   # advance by 10
        bar.set(50)      # jump to 50
        bar.finish()     # print newline and final state
    """

    BAR_WIDTH = 40

    def __init__(self, total: int, prefix: str = "") -> None:
        self.total = max(total, 1)
        self.prefix = prefix
        self._current = 0
        self._render()

    def _render(self) -> None:
        pct = self._current / self.total
        filled = int(self.BAR_WIDTH * pct)
        bar = "█" * filled + "░" * (self.BAR_WIDTH - filled)
        line = f"\r{self.prefix} [{bar}] {self._current}/{self.total} ({pct:.0%})"
        print(line, end="", flush=True, file=sys.stderr)

    def update(self, n: int = 1) -> None:
        self._current = min(self._current + n, self.total)
        self._render()

    def set(self, value: int) -> None:
        self._current = min(value, self.total)
        self._render()

    def set_prefix(self, prefix: str) -> None:
        self.prefix = prefix
        self._render()

    def finish(self) -> None:
        self._current = self.total
        self._render()
        print(file=sys.stderr)  # newline


def status(msg: str) -> None:
    """Print a one-line status message to stderr (no progress bar)."""
    print(f"  {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Argument parsing & helpers
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="RPC-only scanner for counterparties calling a target function directly or internally."
    )
    p.add_argument("--address", required=True, help="Target contract address")
    p.add_argument(
        "--signature",
        required=False,
        default=None,
        help='Canonical signature, e.g. "transfer(address,uint256)". Omit to match any selector.',
    )
    p.add_argument("--days", required=True, type=int, help="Lookback window in days")
    p.add_argument("--rpc-url", required=True, help="Tracing-enabled JSON-RPC endpoint")
    p.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    p.add_argument("--avg-block-time", type=int, default=12, help="Fallback average block time in seconds")
    p.add_argument("--chunk-size", type=int, default=1000, help="Blocks per trace_filter chunk")

    p.add_argument("--verbose-trace-errors", action="store_true", help="Print per-chunk trace errors to stderr")
    return p


def is_hex_address(addr: str) -> bool:
    return isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42


def normalize_hex_address(addr: str) -> str:
    return to_checksum_address(addr)


def function_selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


def hex_to_int(x: str) -> int:
    return int(x, 16)


def int_to_hex(x: int) -> str:
    return hex(x)


def classify_code(code: str) -> str:
    code_lc = (code or "").lower()
    if code_lc in EMPTY_CODES:
        return "[EOA]"
    if code_lc.startswith("0xef0100"):
        return "[7702del]"
    return "[Contract]"


# ---------------------------------------------------------------------------
# RPC primitives
# ---------------------------------------------------------------------------

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
        raise RpcError(f"{method} failed: {data['error']}")
    return data.get("result")


def rpc_batch(
    session: requests.Session,
    rpc_url: str,
    calls: List[Tuple[str, list]],
    timeout: int,
) -> List[Any]:
    """
    Execute a JSON-RPC batch request.

    `calls` is a list of (method, params) tuples.
    Returns a list of result values in the same order as `calls`.
    Entries whose call returned an error are None.
    """
    payload = [
        {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
        for i, (method, params) in enumerate(calls)
    ]
    resp = session.post(rpc_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    items = resp.json()
    # Responses may arrive out of order; use id to reorder.
    ordered = [None] * len(calls)
    for item in items:
        idx = item.get("id")
        if idx is not None and 0 <= idx < len(ordered):
            ordered[idx] = item.get("result")
    return ordered


def assert_trace_filter_supported(
    session: requests.Session,
    rpc_url: str,
    target_contract: str,
    timeout: int,
) -> None:
    try:
        latest = rpc_post(session, rpc_url, "eth_blockNumber", [], timeout)
        if not isinstance(latest, str):
            raise RpcError("eth_blockNumber returned unexpected payload during trace_filter probe")
        latest_int = int(latest, 16)

        params = [{
            "fromBlock": hex(latest_int),
            "toBlock": hex(latest_int),
            "toAddress": [target_contract],
        }]

        result = rpc_post(session, rpc_url, "trace_filter", params, timeout)
        if not isinstance(result, list):
            raise RpcError("trace_filter probe returned unexpected payload")

    except Exception as e:
        raise SystemExit(
            "\nRPC endpoint does not support trace_filter.\n"
            "This scanner requires Erigon/OpenEthereum style trace APIs.\n\n"
            f"RPC URL: {rpc_url}\n"
            f"Error: {e}\n"
        )


def eth_get_code(session: requests.Session, rpc_url: str, address: str, timeout: int) -> str:
    result = rpc_post(session, rpc_url, "eth_getCode", [address, "latest"], timeout)
    if not isinstance(result, str):
        raise RpcError("eth_getCode returned unexpected payload")
    return result


def assert_target_has_code(session: requests.Session, rpc_url: str, address: str, timeout: int) -> None:
    code = eth_get_code(session, rpc_url, address, timeout)
    if classify_code(code) == "[EOA]":
        raise SystemExit(f"Provided address is not a contract-like account: {address}")


def eth_block_number(session: requests.Session, rpc_url: str, timeout: int) -> int:
    result = rpc_post(session, rpc_url, "eth_blockNumber", [], timeout)
    if not isinstance(result, str):
        raise RpcError("eth_blockNumber returned unexpected payload")
    return hex_to_int(result)


def eth_get_block_by_number(
    session: requests.Session,
    rpc_url: str,
    block_number: int,
    timeout: int,
) -> dict:
    result = rpc_post(session, rpc_url, "eth_getBlockByNumber", [int_to_hex(block_number), False], timeout)
    if not isinstance(result, dict):
        raise RpcError("eth_getBlockByNumber returned unexpected payload")
    return result


# ---------------------------------------------------------------------------
# Block range estimation
# ---------------------------------------------------------------------------

def estimate_start_block_by_avg_time(latest_block: int, days: int, avg_block_time: int) -> int:
    blocks_back = int((days * 24 * 60 * 60) / max(avg_block_time, 1))
    return max(0, latest_block - blocks_back)


def refine_start_block_by_timestamp(
    session: requests.Session,
    rpc_url: str,
    latest_block: int,
    target_ts: int,
    rough_start: int,
    timeout: int,
) -> int:
    """Binary search the first block with timestamp >= target_ts."""
    lo = max(0, rough_start)
    hi = latest_block

    blk = eth_get_block_by_number(session, rpc_url, lo, timeout)
    blk_ts = hex_to_int(blk["timestamp"])
    if blk_ts > target_ts:
        lo = 0

    while lo < hi:
        mid = (lo + hi) // 2
        blk = eth_get_block_by_number(session, rpc_url, mid, timeout)
        ts = hex_to_int(blk["timestamp"])
        if ts < target_ts:
            lo = mid + 1
        else:
            hi = mid

    return lo


# ---------------------------------------------------------------------------
# Trace scanning
# ---------------------------------------------------------------------------

def trace_filter_chunk(
    session: requests.Session,
    rpc_url: str,
    from_block: int,
    to_block: int,
    target_contract: str,
    timeout: int,
) -> list:
    params = [{
        "fromBlock": int_to_hex(from_block),
        "toBlock": int_to_hex(to_block),
        "toAddress": [target_contract],
    }]
    result = rpc_post(session, rpc_url, "trace_filter", params, timeout)
    if not isinstance(result, list):
        raise RpcError("trace_filter returned unexpected payload")
    return result


def resolve_tx_froms_batch(
    session: requests.Session,
    rpc_url: str,
    txhashes: List[str],
    cache: Dict[str, Optional[str]],
    timeout: int,
) -> None:
    """
    Fetch tx senders for all `txhashes` not already in `cache`, using a
    single JSON-RPC batch request. Results are written into `cache` in-place.
    """
    missing = [h for h in txhashes if h not in cache]
    if not missing:
        return

    calls = [("eth_getTransactionByHash", [h]) for h in missing]
    results = rpc_batch(session, rpc_url, calls, timeout)

    for txhash, tx in zip(missing, results):
        if tx and isinstance(tx, dict):
            cache[txhash] = tx.get("from")
        else:
            cache[txhash] = None


def add_counterparty(counterparty_to_txs: Dict[str, Set[str]], counterparty: str, txhash: str) -> None:
    if not is_hex_address(counterparty):
        return
    counterparty_to_txs[normalize_hex_address(counterparty)].add(txhash.lower())


def scan_via_trace_filter(
    session: requests.Session,
    rpc_url: str,
    target_contract: str,
    selector: Optional[str],
    start_block: int,
    end_block: int,
    chunk_size: int,
    timeout: int,
    verbose_trace_errors: bool,
) -> Tuple[Dict[str, Set[str]], int, int, int]:
    """
    Returns:
      - counterparty -> unique tx hashes
      - frames_seen
      - selector_matches
      - failed_chunks

    If `selector` is None, every trace reaching the target is counted
    (no selector filtering is performed).
    """
    hits: Dict[str, Set[str]] = defaultdict(set)
    tx_from_cache: Dict[str, Optional[str]] = {}
    frames_seen = 0
    selector_matches = 0
    failed_chunks = 0

    target_lc = target_contract.lower()
    selector_lc = selector.lower() if selector is not None else None

    total_blocks = end_block - start_block + 1
    total_chunks = max(1, (total_blocks + chunk_size - 1) // chunk_size)
    bar = ProgressBar(total=total_chunks, prefix="Scanning blocks")

    current = start_block
    chunk_index = 0

    while current <= end_block:
        chunk_end = min(current + chunk_size - 1, end_block)
        chunk_index += 1
        bar.set_prefix(f"Scanning blocks {current}-{chunk_end}")

        try:
            traces = trace_filter_chunk(
                session=session,
                rpc_url=rpc_url,
                from_block=current,
                to_block=chunk_end,
                target_contract=target_contract,
                timeout=timeout,
            )
        except Exception as e:
            failed_chunks += 1
            if verbose_trace_errors:
                print(
                    f"\n[trace_filter failed] blocks {current}-{chunk_end}: {e}",
                    file=sys.stderr,
                )
            bar.update(1)
            current = chunk_end + 1
            continue

        # ---------------------------------------------------------------
        # First pass: collect all selector-matching traces and gather the
        # txhashes we need to resolve, then batch-fetch them all at once.
        # ---------------------------------------------------------------
        matching_traces = []
        txhashes_needed: List[str] = []

        for tr in traces:
            if not isinstance(tr, dict):
                continue

            frames_seen += 1

            action = tr.get("action") or {}
            txhash = (tr.get("transactionHash") or "").lower()
            to_addr = (action.get("to") or "").lower()
            inp = (action.get("input") or "").lower()

            # We drop the type/callType check entirely: every trace that
            # reaches the target with the right selector is a usage,
            # regardless of call variant (call, staticcall, delegatecall…).
            if to_addr != target_lc:
                continue
            if selector_lc is not None and not inp.startswith(selector_lc):
                continue
            if not txhash:
                continue

            selector_matches += 1
            matching_traces.append(tr)

            if txhash not in tx_from_cache:
                txhashes_needed.append(txhash)

        # Batch-resolve all tx senders needed for this chunk.
        if txhashes_needed:
            resolve_tx_froms_batch(session, rpc_url, txhashes_needed, tx_from_cache, timeout)

        # ---------------------------------------------------------------
        # Second pass: attribute each matching trace to a counterparty.
        #
        # The correct way to distinguish a direct call from an internal
        # one is NOT traceAddress == [] (which is true for any root-level
        # call, including contract-to-contract ones). Instead, compare
        # action.from against the tx origin:
        #   - If they match  → the tx sender called the function directly.
        #   - If they differ → an intermediate contract (action.from) is
        #                       the immediate caller we want to record.
        # ---------------------------------------------------------------
        for tr in matching_traces:
            action = tr.get("action") or {}
            txhash = (tr.get("transactionHash") or "").lower()
            frm = action.get("from") or ""

            top_from = tx_from_cache.get(txhash)

            is_direct = top_from and (top_from.lower() == frm.lower())

            if is_direct:
                add_counterparty(hits, top_from, txhash)
            else:
                # Internal call: record the immediate contract caller.
                add_counterparty(hits, frm, txhash)

        bar.update(1)
        current = chunk_end + 1
        time.sleep(0.05)

    bar.finish()
    return hits, frames_seen, selector_matches, failed_chunks


# ---------------------------------------------------------------------------
# Address classification
# ---------------------------------------------------------------------------

def classify_addresses_batch(
    session: requests.Session,
    rpc_url: str,
    addresses: Iterable[str],
    timeout: int,
) -> Dict[str, str]:
    """
    Classify all addresses in a single JSON-RPC batch call.
    """
    addr_list = sorted(set(addresses), key=str.lower)
    if not addr_list:
        return {}

    bar = ProgressBar(total=len(addr_list), prefix="Classifying addresses")

    calls = [("eth_getCode", [addr, "latest"]) for addr in addr_list]
    results = rpc_batch(session, rpc_url, calls, timeout)

    out: Dict[str, str] = {}
    for addr, code in zip(addr_list, results):
        out[addr] = classify_code(code or "0x")
        bar.update(1)

    bar.finish()
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    args = build_parser().parse_args()

    if args.days < 0:
        raise SystemExit("--days must be >= 0")
    if args.chunk_size <= 0:
        raise SystemExit("--chunk-size must be > 0")
    if not is_hex_address(args.address):
        raise SystemExit(f"Invalid target address: {args.address}")

    target_contract = normalize_hex_address(args.address)
    selector = function_selector(args.signature) if args.signature else None

    now_dt = datetime.now(timezone.utc)
    cutoff_dt = now_dt - timedelta(days=args.days)
    cutoff_ts = int(cutoff_dt.timestamp())

    session = requests.Session()
    session.headers.update({"User-Agent": "usage-scanner-rpc-only/1.2"})

    status("Checking trace_filter support…")
    assert_trace_filter_supported(
        session=session,
        rpc_url=args.rpc_url,
        target_contract=target_contract,
        timeout=args.timeout,
    )

    status("Verifying target has code…")
    assert_target_has_code(session, args.rpc_url, target_contract, args.timeout)

    status("Resolving block range…")
    latest_block = eth_block_number(session, args.rpc_url, args.timeout)
    rough_start = estimate_start_block_by_avg_time(latest_block, args.days, args.avg_block_time)
    start_block = refine_start_block_by_timestamp(
        session=session,
        rpc_url=args.rpc_url,
        latest_block=latest_block,
        target_ts=cutoff_ts,
        rough_start=rough_start,
        timeout=args.timeout,
    )
    status(f"Block range: {start_block} → {latest_block}")

    hits, frames_seen, selector_matches, failed_chunks = scan_via_trace_filter(
        session=session,
        rpc_url=args.rpc_url,
        target_contract=target_contract,
        selector=selector,
        start_block=start_block,
        end_block=latest_block,
        chunk_size=args.chunk_size,
        timeout=args.timeout,
        verbose_trace_errors=args.verbose_trace_errors,
    )

    labels = classify_addresses_batch(
        session=session,
        rpc_url=args.rpc_url,
        addresses=hits.keys(),
        timeout=args.timeout,
    )

    total_matched_txs = sum(len(v) for v in hits.values())
    total_unique_counterparties = len(hits)

    print()
    print(f"#####################################")
    print(f"############# RESULTS ###############")
    print(f"#####################################")
    print(f"# Contract: {target_contract}")
    print(f"# Function signature: {args.signature if args.signature else '<any>'}")
    print(f"# Selector: {selector if selector else '<any>'}")
    print(f"# Lookback days: {args.days}")
    print(f"# Cutoff UTC: {cutoff_dt.isoformat()}")
    print(f"# Start block: {start_block}")
    print(f"# End block: {latest_block}")
    print(f"# Trace frames seen: {frames_seen}")
    print(f"# Selector-matching frames: {selector_matches}")
    print(f"# trace_filter failed chunks: {failed_chunks}")
    print(f"# Matched txs: {total_matched_txs}")
    print(f"# Unique counterparties: {total_unique_counterparties}")
    print()

    rows = []
    for addr, txhashes in hits.items():
        label = labels.get(addr, "[EOA]")
        rows.append((label, addr, len(txhashes)))

    rows.sort(key=lambda x: (-x[2], x[1].lower()))

    for label, addr, n in rows:
        print(f"{label} {addr} ({n} txs)")

    return 0


if __name__ == "__main__":
    sys.exit(main())