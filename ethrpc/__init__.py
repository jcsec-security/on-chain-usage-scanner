"""
Shared Ethereum JSON-RPC helpers for find_address_refs.py and
on_chain_target_interactions.py.

This module is deliberately small and dependency-light (only `requests`).
It wraps JSON-RPC primitives, block-range resolution, trace_filter helpers,
and bytecode/account classification.

LIMITATIONS (common to both callers):
  * Mainnet-oriented defaults (12s avg block time, standard RPC semantics).
    Adapting to other chains requires updating avg_block_time and any
    chain-specific assumptions at the call sites.
  * `trace_filter` is not a standard JSON-RPC method; it's an Erigon /
    OpenEthereum extension. Call assert_trace_filter_supported() at the
    start of any trace-based scan so the failure happens up front rather
    than mid-scan.
  * No retry logic. A transient network error propagates as an exception
    — callers can wrap with their own retry policy if needed.
  * No pagination inside trace_filter_chunk: if a single chunk returns
    too much data for your provider, shrink the chunk size at the call
    site rather than inside the helper.
"""
from .client import (
    RpcError,
    hex_to_int,
    int_to_hex,
    make_session,
    rpc_batch,
    rpc_post,
)
from .blocks import (
    estimate_start_block_by_avg_time,
    get_block_by_number,
    latest_block,
    refine_start_block_by_timestamp,
    resolve_window,
)
from .traces import (
    assert_trace_filter_supported,
    count_unique_txs_per_target,
    iter_chunks,
    resolve_tx_froms_batch,
    trace_filter_chunk,
)
from .codes import (
    classify_addresses_batch,
    classify_code,
    eth_get_code,
)

__all__ = [
    # client
    "RpcError", "make_session", "rpc_post", "rpc_batch", "hex_to_int", "int_to_hex",
    # blocks
    "latest_block", "get_block_by_number", "resolve_window",
    "estimate_start_block_by_avg_time", "refine_start_block_by_timestamp",
    # traces
    "assert_trace_filter_supported", "trace_filter_chunk", "iter_chunks",
    "count_unique_txs_per_target", "resolve_tx_froms_batch",
    # codes
    "classify_code", "eth_get_code", "classify_addresses_batch",
]
