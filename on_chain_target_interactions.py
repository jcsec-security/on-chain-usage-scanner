#!/usr/bin/env python3
"""
RPC-only usage scanner for direct + internal calls to a target function.

PURPOSE
-------
Given a contract address and (optionally) a function signature, list every
unique counterparty that has called that function on the contract over the
last N days, whether as a direct tx.from or via an internal call from
another contract.

Requirements:
    pip install requests eth-utils 'eth-hash[pycryptodome]'

Usage:
    python on_chain_target_interactions.py \\
        --address 0xYourContract \\
        --signature "requestL2Transaction(address,uint256,bytes,uint256,uint256,bytes[],address)" \\
        --days 14 \\
        --rpc-url https://your-tracing-rpc.example

What it does:
* Verifies the RPC provider supports trace_filter (Erigon/OpenEthereum style).
* Verifies the target address has code via eth_getCode.
* Computes a block range for the last N days (binary search on timestamps).
* Uses trace_filter(toAddress=[target]) over that range, chunked.
* Filters frames by function selector (first 4 bytes of action.input).
* Attributes each matching frame to a counterparty:
    - direct call:    tx.from (resolved via eth_getTransactionByHash)
    - internal call:  action.from
* Classifies counterparties as [EOA] / [Contract] / [7702del] via eth_getCode.

LIMITATIONS / FALSE NEGATIVES
-----------------------------
* Time-bounded by --days only. Requires a tracing-enabled RPC that retains
  enough history for the window.
* Selector must match exactly. A contract calling the target via a wrapper
  function (different selector) will not register against the --signature.
  Drop --signature to see all callers of any function on the contract.
* Delegatecall semantics: we count frames by action.to matching the target.
  A delegatecall FROM the target to another contract is not a "call to the
  target" and is correctly not counted. A delegatecall TO the target (rare
  in practice) would be counted.
* Failed trace_filter chunks are logged and skipped. Counterparties whose
  txs land only in failed chunks are undercounted without precise warning.

OUTPUT
------
Prints a ranked list like:
    [Contract] 0xabc....ff (n txs)
    [EOA]      0x123....aa (n txs)
    [7702del]  0x456....11 (n txs)

`n txs` = number of UNIQUE transaction hashes per counterparty (a tx that
hits the target 10 times internally counts once).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict

from eth_utils import keccak, to_checksum_address

from ethrpc import (
    RpcError,
    assert_trace_filter_supported,
    classify_addresses_batch,
    eth_get_code,
    classify_code,
    make_session,
    resolve_tx_froms_batch,
    resolve_window,
    trace_filter_chunk,
)


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------

class ProgressBar:
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
        print(
            f"\r{self.prefix} [{bar}] {self._current}/{self.total} ({pct:.0%})",
            end="", flush=True, file=sys.stderr,
        )

    def update(self, n: int = 1) -> None:
        self._current = min(self._current + n, self.total)
        self._render()

    def set_prefix(self, prefix: str) -> None:
        self.prefix = prefix
        self._render()

    def finish(self) -> None:
        self._current = self.total
        self._render()
        print(file=sys.stderr)


def status(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_hex_address(addr: str) -> bool:
    return isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42


def function_selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


def add_counterparty(
    counterparty_to_txs: dict[str, set[str]], counterparty: str, txhash: str,
) -> None:
    if not is_hex_address(counterparty):
        return
    counterparty_to_txs[to_checksum_address(counterparty)].add(txhash.lower())


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def scan_via_trace_filter(
    session,
    rpc_url: str,
    target_contract: str,
    selector: str | None,
    start_block: int,
    end_block: int,
    chunk_size: int,
    timeout: int,
    verbose_trace_errors: bool,
) -> tuple[dict[str, set[str]], int, int, int]:
    hits: dict[str, set[str]] = defaultdict(set)
    tx_from_cache: dict[str, str | None] = {}
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
                session, rpc_url, current, chunk_end,
                to_addresses=[target_contract], timeout=timeout,
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

        # First pass: collect matching traces and gather tx hashes to resolve.
        matching_traces = []
        txhashes_needed: list[str] = []

        for tr in traces:
            if not isinstance(tr, dict):
                continue
            frames_seen += 1

            action = tr.get("action") or {}
            txhash = (tr.get("transactionHash") or "").lower()
            to_addr = (action.get("to") or "").lower()
            inp = (action.get("input") or "").lower()

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

        if txhashes_needed:
            resolve_tx_froms_batch(
                session, rpc_url, txhashes_needed, tx_from_cache, timeout,
            )

        # Second pass: attribute to direct vs internal caller.
        for tr in matching_traces:
            action = tr.get("action") or {}
            txhash = (tr.get("transactionHash") or "").lower()
            frm = action.get("from") or ""
            top_from = tx_from_cache.get(txhash)

            if top_from and top_from.lower() == frm.lower():
                add_counterparty(hits, top_from, txhash)
            else:
                add_counterparty(hits, frm, txhash)

        bar.update(1)
        current = chunk_end + 1
        time.sleep(0.05)

    bar.finish()
    return hits, frames_seen, selector_matches, failed_chunks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="RPC-only scanner for counterparties calling a target function directly or internally."
    )
    p.add_argument("--address", required=True, help="Target contract address")
    p.add_argument("--signature", default=None,
                   help='Canonical signature, e.g. "transfer(address,uint256)". Omit to match any selector.')
    p.add_argument("--days", required=True, type=int, help="Lookback window in days")
    p.add_argument("--rpc-url", required=True, help="Tracing-enabled JSON-RPC endpoint")
    p.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    p.add_argument("--avg-block-time", type=int, default=12, help="Fallback average block time in seconds")
    p.add_argument("--chunk-size", type=int, default=1000, help="Blocks per trace_filter chunk")
    p.add_argument("--verbose-trace-errors", action="store_true",
                   help="Print per-chunk trace errors to stderr")
    return p


def main() -> int:
    args = build_parser().parse_args()

    if args.days < 0:
        raise SystemExit("--days must be >= 0")
    if args.chunk_size <= 0:
        raise SystemExit("--chunk-size must be > 0")
    if not is_hex_address(args.address):
        raise SystemExit(f"Invalid target address: {args.address}")

    target_contract = to_checksum_address(args.address)
    selector = function_selector(args.signature) if args.signature else None

    session = make_session("usage-scanner/2.0")

    status("Checking trace_filter support...")
    try:
        assert_trace_filter_supported(session, args.rpc_url, target_contract, args.timeout)
    except RpcError as e:
        raise SystemExit(
            "\nRPC endpoint does not support trace_filter.\n"
            "This scanner requires Erigon/OpenEthereum-style trace APIs.\n\n"
            f"RPC URL: {args.rpc_url}\nError: {e}\n"
        )

    status("Verifying target has code...")
    code = eth_get_code(session, args.rpc_url, target_contract, args.timeout)
    if classify_code(code) == "[EOA]":
        raise SystemExit(f"Provided address is not a contract-like account: {target_contract}")

    status("Resolving block range...")
    start_block, end_block = resolve_window(
        session, args.rpc_url, args.days, args.avg_block_time, args.timeout,
    )
    status(f"Block range: {start_block} → {end_block}")

    hits, frames_seen, selector_matches, failed_chunks = scan_via_trace_filter(
        session=session,
        rpc_url=args.rpc_url,
        target_contract=target_contract,
        selector=selector,
        start_block=start_block,
        end_block=end_block,
        chunk_size=args.chunk_size,
        timeout=args.timeout,
        verbose_trace_errors=args.verbose_trace_errors,
    )

    labels = classify_addresses_batch(
        session, args.rpc_url, hits.keys(), args.timeout,
    )

    total_matched_txs = sum(len(v) for v in hits.values())
    print()
    print("#####################################")
    print("############# RESULTS ###############")
    print("#####################################")
    print(f"# Contract: {target_contract}")
    print(f"# Function signature: {args.signature if args.signature else '<any>'}")
    print(f"# Selector: {selector if selector else '<any>'}")
    print(f"# Lookback days: {args.days}")
    print(f"# Start block: {start_block}")
    print(f"# End block: {end_block}")
    print(f"# Trace frames seen: {frames_seen}")
    print(f"# Selector-matching frames: {selector_matches}")
    print(f"# trace_filter failed chunks: {failed_chunks}")
    print(f"# Matched txs: {total_matched_txs}")
    print(f"# Unique counterparties: {len(hits)}")
    print()

    rows = [(labels.get(a, "[EOA]"), a, len(txs)) for a, txs in hits.items()]
    rows.sort(key=lambda x: (-x[2], x[1].lower()))
    for label, addr, n in rows:
        print(f"{label} {addr} ({n} txs)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
