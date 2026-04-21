"""
Shared Ethereum JSON-RPC helpers used by find_address_refs.py and
on_chain_target_interactions.py.

Contents:
  client.py  — session, rpc_post, rpc_batch, RpcError, hex helpers
  blocks.py  — latest_block, get_block_by_number, resolve_window
               (binary-searches the block-timestamp boundary)
  traces.py  — trace_filter probe, chunked scans, unique-tx counter,
               tx-sender batch resolver
  codes.py   — classify_code: EOA / Contract / EIP-7702 delegation

Only dependency is `requests`. Callers provide timeouts and concurrency;
no retry logic is built in. `trace_filter` is an Erigon/OpenEthereum
extension — call assert_trace_filter_supported() before scanning.
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
