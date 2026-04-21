"""trace_filter helpers: support probe, chunked scans, unique-tx counting per target."""

from __future__ import annotations

import concurrent.futures
import sys
from collections import defaultdict
from typing import Callable, Iterable, Iterator

import requests

from .client import RpcError, int_to_hex, rpc_batch, rpc_post
from .blocks import latest_block


def assert_trace_filter_supported(
    session: requests.Session,
    rpc_url: str,
    probe_address: str,
    timeout: int,
) -> None:
    """Runs a trivial trace_filter call to confirm the RPC supports it."""
    try:
        lb = latest_block(session, rpc_url, timeout)
        params = [{
            "fromBlock": int_to_hex(lb),
            "toBlock":   int_to_hex(lb),
            "toAddress": [probe_address],
        }]
        result = rpc_post(session, rpc_url, "trace_filter", params, timeout)
        if not isinstance(result, list):
            raise RpcError("trace_filter probe returned unexpected payload")
    except Exception as e:
        raise RpcError(f"trace_filter not supported by {rpc_url}: {e}") from e


def trace_filter_chunk(
    session: requests.Session,
    rpc_url: str,
    from_block: int,
    to_block: int,
    to_addresses: list[str],
    timeout: int,
    from_addresses: list[str] | None = None,
) -> list:
    """Single trace_filter call over a block range. Returns the raw trace list."""
    params_obj: dict = {
        "fromBlock": int_to_hex(from_block),
        "toBlock":   int_to_hex(to_block),
        "toAddress": to_addresses,
    }
    if from_addresses is not None:
        params_obj["fromAddress"] = from_addresses
    result = rpc_post(session, rpc_url, "trace_filter", [params_obj], timeout)
    if not isinstance(result, list):
        raise RpcError("trace_filter returned unexpected payload")
    return result


def iter_chunks(from_block: int, to_block: int, chunk_size: int) -> Iterator[tuple[int, int]]:
    cur = from_block
    while cur <= to_block:
        end = min(cur + chunk_size - 1, to_block)
        yield cur, end
        cur = end + 1


def count_unique_txs_per_target(
    session: requests.Session,
    rpc_url: str,
    targets: Iterable[str],
    from_block: int,
    to_block: int,
    chunk_size: int = 1000,
    timeout: int = 60,
    workers: int = 1,
    on_chunk: Callable[[int, int, int, int, int, int], None] | None = None,
) -> tuple[dict[str, set[str]], int]:
    """
    Scan trace_filter over [from_block, to_block] with toAddress=targets,
    chunked by `chunk_size` blocks. Counts UNIQUE tx hashes per target —
    both direct and internal calls contribute, a tx that hits the target
    N times counts once.

    Returns (tx_map, failed_chunks) where tx_map[target_lowercase] is the
    set of unique tx hashes. Failed chunks are counted; their block ranges
    contribute zero rather than raising — callers should surface the count.

    workers > 1 runs chunks concurrently. on_chunk, if provided, is called
    once per completed chunk with (idx, total, from, to, running_txs, failed).
    """
    target_set = {t.lower() for t in targets}
    if not target_set:
        return {}, 0

    tx_per_target: dict[str, set[str]] = defaultdict(set)
    chunks = list(iter_chunks(from_block, to_block, chunk_size))
    total = len(chunks)

    if workers <= 1:
        return _scan_sequential(
            session, rpc_url, target_set, chunks, timeout, tx_per_target, on_chunk,
        )
    return _scan_parallel(
        session, rpc_url, target_set, chunks, timeout, workers, tx_per_target, on_chunk,
    )


def _absorb_traces(
    traces: list, target_set: set[str], tx_per_target: dict[str, set[str]],
) -> None:
    for tr in traces:
        if not isinstance(tr, dict):
            continue
        action = tr.get("action") or {}
        to_addr = (action.get("to") or "").lower()
        txhash = (tr.get("transactionHash") or "").lower()
        if to_addr in target_set and txhash:
            tx_per_target[to_addr].add(txhash)


def _scan_sequential(
    session, rpc_url, target_set, chunks, timeout, tx_per_target, on_chunk,
) -> tuple[dict[str, set[str]], int]:
    failed = 0
    total = len(chunks)
    first_errors: list[str] = []
    for i, (a, b) in enumerate(chunks, 1):
        try:
            traces = trace_filter_chunk(session, rpc_url, a, b, list(target_set), timeout)
            _absorb_traces(traces, target_set, tx_per_target)
        except Exception as e:
            failed += 1
            # Log first 3 failures with the exception class and message so the
            # user can see WHY chunks are failing, not just that they are.
            if len(first_errors) < 3:
                msg = f"  chunk {i} (blocks {a}-{b}) failed: {type(e).__name__}: {e}"
                print(msg, file=sys.stderr)
                first_errors.append(msg)
            elif len(first_errors) == 3:
                print("  (further chunk errors suppressed)", file=sys.stderr)
                first_errors.append("...")  # sentinel so we don't print again
        if on_chunk:
            running = sum(len(v) for v in tx_per_target.values())
            on_chunk(i, total, a, b, running, failed)
    return dict(tx_per_target), failed


def _scan_parallel(
    session, rpc_url, target_set, chunks, timeout, workers, tx_per_target, on_chunk,
) -> tuple[dict[str, set[str]], int]:
    failed = 0
    total = len(chunks)
    first_errors: list[str] = []

    def scan_one(idx: int, a: int, b: int) -> tuple[int, int, int, list | None, str | None]:
        try:
            traces = trace_filter_chunk(session, rpc_url, a, b, list(target_set), timeout)
            return idx, a, b, traces, None
        except Exception as e:
            return idx, a, b, None, f"{type(e).__name__}: {e}"

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(scan_one, i, a, b) for i, (a, b) in enumerate(chunks, 1)]
        for fut in concurrent.futures.as_completed(futures):
            idx, a, b, traces, err = fut.result()
            if traces is None:
                failed += 1
                if err and len(first_errors) < 3:
                    msg = f"  chunk {idx} (blocks {a}-{b}) failed: {err}"
                    print(msg, file=sys.stderr)
                    first_errors.append(msg)
                elif len(first_errors) == 3:
                    print("  (further chunk errors suppressed)", file=sys.stderr)
                    first_errors.append("...")
            else:
                _absorb_traces(traces, target_set, tx_per_target)
            done += 1
            if on_chunk:
                running = sum(len(v) for v in tx_per_target.values())
                on_chunk(done, total, a, b, running, failed)
    return dict(tx_per_target), failed


def resolve_tx_froms_batch(
    session: requests.Session,
    rpc_url: str,
    txhashes: list[str],
    cache: dict[str, str | None],
    timeout: int,
) -> None:
    """
    Fill `cache[txhash] = tx['from']` for every hash not yet resolved, in a
    single JSON-RPC batch request. Mutates `cache` in place.
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