"""trace_filter helpers: support probe, chunked scans, unique-tx counting per target."""

from __future__ import annotations

import concurrent.futures
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
    chunked by `chunk_size` blocks. Counts unique transaction hashes per
    target — both direct (external) and internal calls that reach the
    target contribute.

    Returns (tx_map, failed_chunks) where tx_map[target_lowercase] = set
    of unique transaction hashes.

    KEY SEMANTICS:
    * UNIQUE txs, not frames. A tx that hits a target 100 times via
      internal calls counts as ONE.
    * Scan is over a FIXED block range snapshot. Transactions mined
      AFTER from_block..to_block was captured are not included.
    * Failed chunks are counted but not retried. A persistent failure
      in a chunk means candidates whose txs land mostly there will be
      undercounted with no warning other than the failed_chunks count.

    PARALLELISM (`workers` > 1):
    * Runs chunks concurrently through a thread pool.
    * Trace providers vary in concurrency tolerance; on Chainstack
      tracing tier 4-6 workers is usually safe, Erigon can handle more.
    * If you see rate-limit errors, drop workers to 1 first, then try
      smaller chunk_size.
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
    for i, (a, b) in enumerate(chunks, 1):
        try:
            traces = trace_filter_chunk(session, rpc_url, a, b, list(target_set), timeout)
            _absorb_traces(traces, target_set, tx_per_target)
        except Exception:
            failed += 1
        if on_chunk:
            running = sum(len(v) for v in tx_per_target.values())
            on_chunk(i, total, a, b, running, failed)
    return dict(tx_per_target), failed


def _scan_parallel(
    session, rpc_url, target_set, chunks, timeout, workers, tx_per_target, on_chunk,
) -> tuple[dict[str, set[str]], int]:
    failed = 0
    total = len(chunks)

    def scan_one(idx: int, a: int, b: int) -> tuple[int, int, int, list | None]:
        try:
            traces = trace_filter_chunk(session, rpc_url, a, b, list(target_set), timeout)
            return idx, a, b, traces
        except Exception:
            return idx, a, b, None

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(scan_one, i, a, b) for i, (a, b) in enumerate(chunks, 1)]
        for fut in concurrent.futures.as_completed(futures):
            idx, a, b, traces = fut.result()
            if traces is None:
                failed += 1
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
