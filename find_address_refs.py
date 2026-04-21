"""
Find contracts on Ethereum mainnet that reference a specific target address.

PIPELINE STAGES
===============
1. Dune SQL queries (4 sources, run in parallel):
     - bytecode      : target bytes appear in a contract's creation code
                       (catches `address constant`, `immutable`, and
                       constructor arguments).
     - tx_input      : target (padded to 32 bytes) appears anywhere in the
                       calldata of a tx sent to the contract (catches
                       externally-triggered setters and configuration calls).
     - trace_input   : target appears in the input of an internal call to
                       the contract (catches factory-initialized contracts
                       and proxies/routers passing the address along).
     - event_log     : target appears as an indexed topic or in non-indexed
                       data of an event emitted by the contract.

2. Activity filter (opt-in, via --min-txs):
     Counts unique transactions (direct + internal) that hit each candidate
     in the last --window-days days, by scanning `trace_filter` on a
     tracing-enabled RPC. Drops candidates below --min-txs.

3. Storage verification (DEFAULT ON; --no-verify to disable):
     Reads storage slots 0..--verify-slots-1 of each candidate via
     eth_getStorageAt and checks whether the target's 20-byte pattern
     appears anywhere in the 32-byte slot value. Drops candidates whose
     current storage doesn't hold the address — with the "bytecode-only
     exemption" documented below.

WHAT IT FINDS (high-signal matches)
===================================
* Contracts with the target hardcoded as `address constant` or
  `immutable` (compiled into runtime bytecode).
* Contracts deployed with the target as a constructor argument.
* Contracts with a setter function called externally OR from another
  contract, passing the target address as an argument.
* Contracts that emitted an event containing the target address. This is
  typical of admin/owner/oracle/router setters.
* Contracts that currently store the target address in one of the first
  --verify-slots storage slots (default 50).

WHAT IT MISSES (known false negatives)
======================================
* References older than 365 days in tx_input / trace_input / event_log.
  The SQL queries are bounded to keep scans tractable on Dune's free
  tier. The bytecode query is NOT time-bounded, so ancient deploys with
  hardcoded references are still caught.

* Addresses stored in Solidity mappings: `mapping(anything => address)`.
  A mapping entry's slot is keccak256(key . baseSlot), which is outside
  the linear 0..--verify-slots range. The storage verify step cannot
  reach them. If your target lives in mappings across many contracts,
  run with --no-verify.

* Addresses stored in structs or dynamic arrays past slot
  (--verify-slots - 1). Raise --verify-slots if the target protocol uses
  large state layouts; the cost is linear in slots × candidates.

* Proxy implementation addresses at EIP-1967 slots. These live at
    - impl  = 0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc
    - admin = 0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103
  far outside 0..50. A proxy that delegates to the target implementation
  will NOT match the linear slot scan. Use a proxy-aware tool (Etherscan,
  proxy detectors) if this case matters.

* Contracts that obfuscate the address (XOR with a constant, hash-derived
  lookups, sum of two stored halves, etc.). The bytecode scan matches
  only the raw 20-byte literal; encoded references pass through undetected.

* Addresses passed only via ERC-20/721/1155 standard functions.
  We exclude these 4-byte selectors from tx_input and trace_input:
      0xa9059cbb transfer(address,uint256)
      0x23b872dd transferFrom(address,address,uint256)
      0x095ea7b3 approve(address,uint256)
      0xa22cb465 setApprovalForAll(address,bool)
      0x42842e0e safeTransferFrom(address,address,uint256)
      0xb88d4fde safeTransferFrom(address,address,uint256,bytes)
      0xf242432a safeTransferFrom(address,address,uint256,uint256,bytes)
      0x2eb2c2d6 safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)
  NOTE: ERC-20 allowances to the target ARE technically stored in state
  (the allowances mapping), but we treat these as counterparty noise, not
  integration references. If you specifically want contracts that granted
  approval to the target, remove 0x095ea7b3 from the SQL exclusion list.

* Standard token events excluded from event_log (same reasoning):
      Transfer, Approval, ApprovalForAll, TransferSingle, TransferBatch

* permit() (EIP-2612) is NOT filtered — the spender address will appear
  in tx_input and event_log hits. Depending on your use case this may be
  noise (per-user approval) or signal (protocol-level integration).

WHAT MAY BE OVER-INCLUDED (known false positives)
=================================================
* Raw 20-byte coincidental matches in bytecode. Probability ~1 in 2^160 —
  effectively zero, but technically nonzero.

* Contracts that once wrote the target address to storage and later
  overwrote it to zero. The Dune queries surface historical writes.
  With --verify on (default), these are caught and dropped. With
  --no-verify, they remain in output.

* Addresses packed into a storage slot with other data. The storage check
  matches the 20-byte pattern anywhere in the 32-byte slot, so the slot
  index in the CSV may correspond to a packed field rather than the
  "conceptual" address variable.

* Heavy integrations of the target address (e.g., WETH, USDC) will
  produce large result sets. Use --min-txs to focus on actively-used
  contracts and/or narrow --sources to just bytecode for a cleaner
  "baked-in reference" view.

BYTECODE-ONLY EXEMPTION
=======================
A candidate whose ONLY Dune signal is the bytecode source is exempt
from the storage verification step and kept in output with
`storage_slots_matched = exempt`.

Rationale: `address constant` and `immutable` declarations are patched
by solc into the runtime bytecode at deploy time and NEVER enter the
storage trie. eth_getStorageAt cannot find them at any slot. Without
this exemption, the default storage filter would reliably drop these
correctly-identified contracts.

Trade-offs in the exemption logic:
* Lenient: constructor arguments stored to state at slot >= --verify-slots
  are also bytecode-only and exempted, even though a larger --verify-slots
  would have found them. We err toward inclusion.
* Strict when multi-sourced: a contract appearing in bytecode AND any
  runtime source (tx_input / trace_input / event_log) is NOT bytecode-only
  and gets verified. This correctly handles constructor args stored in
  state variables that then emit a setter event.

REQUIREMENTS AND ENV VARS
=========================
DUNE_API_KEY           : Dune Analytics API key (paid plan recommended
                         for any non-trivial scan volume).
ETH_RPC_URL            : Ethereum mainnet RPC URL.
                         - For --verify (default on): any RPC that supports
                           eth_getStorageAt (Alchemy, QuickNode, Infura…).
                         - For --min-txs > 0: must additionally support
                           trace_filter (Erigon / OpenEthereum /
                           Chainstack tracing tier / Alchemy trace add-on).

Saved Dune queries: four queries per dune_queries.sql, accepting
{{target_address_raw}} and {{target_address_padded}} parameters. Put
their IDs in DEFAULT_QUERY_IDS below or pass via --query-* CLI flags.

OUT OF SCOPE
============
* Non-mainnet chains. SQL uses `ethereum.*` tables and Etherscan URLs
  point at etherscan.io. Adapting requires changing both.
* Historical state queries. Storage verify reads "latest" only.
* Arbitrary string matching. This tool is specific to 20-byte addresses.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

DUNE_BASE = "https://api.dune.com/api/v1"
ETHERSCAN_URL_BASE = "https://etherscan.io"  # only used to build clickable links

POLL_INTERVAL_S = 3
EXECUTION_TIMEOUT_S = 30 * 60

ALL_SOURCES = ("bytecode", "tx_input", "trace_input", "event_log")

# ---------------------------------------------------------------------------
# Default Dune query IDs — EDIT THESE to match your saved queries.
# Set to 0 for any source you haven't saved yet; --query-* CLI flags override.
# ---------------------------------------------------------------------------
DEFAULT_QUERY_IDS: dict[str, int] = {
    "bytecode":    0,   # e.g. 1234567
    "tx_input":    0,
    "trace_input": 0,
    "event_log":   0,
}


# ---------------------------------------------------------------------------
# Address utils
# ---------------------------------------------------------------------------

def normalize_addr(addr: str) -> tuple[str, str]:
    """Returns (raw_20byte_hex, padded_32byte_hex), both lowercase with 0x."""
    a = addr.lower()
    if a.startswith("0x"):
        a = a[2:]
    if len(a) != 40:
        sys.exit(f"expected 20-byte address, got {addr!r}")
    try:
        int(a, 16)
    except ValueError:
        sys.exit(f"address is not valid hex: {addr!r}")
    return "0x" + a, "0x" + ("0" * 24) + a


def etherscan_addr_url(addr: str) -> str:
    return f"{ETHERSCAN_URL_BASE}/address/{addr}"


def etherscan_tx_url(tx: str) -> str:
    return f"{ETHERSCAN_URL_BASE}/tx/{tx}"


# ---------------------------------------------------------------------------
# Dune client
# ---------------------------------------------------------------------------

class DuneError(RuntimeError):
    pass


class DuneClient:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({"X-Dune-API-Key": api_key})

    def execute(self, query_id: int, params: dict[str, str]) -> str:
        r = self.session.post(
            f"{DUNE_BASE}/query/{query_id}/execute",
            json={"query_parameters": params},
            timeout=30,
        )
        if r.status_code != 200:
            raise DuneError(f"execute {query_id} failed: {r.status_code} {r.text}")
        return r.json()["execution_id"]

    def wait(self, execution_id: str, timeout_s: int = EXECUTION_TIMEOUT_S) -> None:
        start = time.time()
        while True:
            r = self.session.get(
                f"{DUNE_BASE}/execution/{execution_id}/status", timeout=30,
            )
            if r.status_code != 200:
                raise DuneError(f"status failed: {r.status_code} {r.text}")
            state = r.json().get("state", "")
            if state == "QUERY_STATE_COMPLETED":
                return
            if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"):
                raise DuneError(f"execution {execution_id} ended: {state}")
            if time.time() - start > timeout_s:
                raise DuneError(f"execution {execution_id} timed out")
            time.sleep(POLL_INTERVAL_S)

    def results(self, execution_id: str) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        limit = 1000
        while True:
            r = self.session.get(
                f"{DUNE_BASE}/execution/{execution_id}/results",
                params={"limit": limit, "offset": offset},
                timeout=60,
            )
            if r.status_code != 200:
                raise DuneError(f"results failed: {r.status_code} {r.text}")
            batch = r.json().get("result", {}).get("rows", [])
            rows.extend(batch)
            if len(batch) < limit:
                return rows
            offset += limit

    def run(self, query_id: int, params: dict[str, str]) -> list[dict]:
        eid = self.execute(query_id, params)
        self.wait(eid)
        return self.results(eid)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class Hit:
    source: str
    contract_address: str
    tx_hash: str


# ---------------------------------------------------------------------------
# Query orchestration
# ---------------------------------------------------------------------------

def run_queries(
    dune_key: str,
    query_ids: dict[str, int],
    target_raw: str,
    target_padded: str,
) -> dict[str, list[Hit]]:
    """Runs selected queries in parallel. Returns source -> list of Hits."""
    params = {"target_address_raw": target_raw, "target_address_padded": target_padded}
    dune = DuneClient(dune_key)

    def run_one(source: str, qid: int) -> tuple[str, list[Hit]]:
        print(f"[{source}] executing query {qid}...", flush=True)
        t0 = time.time()
        rows = dune.run(qid, params)
        print(f"[{source}] {len(rows)} rows in {time.time() - t0:.1f}s", flush=True)
        hits = [
            Hit(
                source=source,
                contract_address=str(r["contract_address"]).lower(),
                tx_hash=str(r["tx_hash"]).lower(),
            )
            for r in rows
        ]
        return source, hits

    by_source: dict[str, list[Hit]] = {s: [] for s in query_ids}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(query_ids)) as pool:
        futures = {pool.submit(run_one, s, qid): s for s, qid in query_ids.items()}
        for fut in concurrent.futures.as_completed(futures):
            src = futures[fut]
            try:
                source, hits = fut.result()
                by_source[source] = hits
            except DuneError as e:
                print(f"[{src}] ERROR: {e}", file=sys.stderr)
    return by_source


# ---------------------------------------------------------------------------
# Activity filter via trace_filter (counts external + internal txs).
# Requires a tracing-enabled RPC (Erigon / OpenEthereum / Chainstack-style).
# ---------------------------------------------------------------------------

from ethrpc import (
    RpcError,
    assert_trace_filter_supported,
    count_unique_txs_per_target,
    make_session,
    resolve_window,
    rpc_post,
)


def filter_by_activity(
    contracts: list[str],
    rpc_url: str,
    min_txs: int,
    window_days: int,
    chunk_size: int,
    timeout: int,
    avg_block_time: int,
    trace_workers: int = 1,
) -> set[str]:
    """
    Count unique transactions hitting each contract in the last `window_days`
    via trace_filter, and return the subset meeting `min_txs`.

    Counts BOTH direct (external) and internal calls uniformly: any trace
    frame whose `action.to` equals the contract contributes its tx hash.
    The metric is `len(set(tx_hashes))` per contract — a tx that hits the
    contract via 100 internal calls counts as ONE.

    Caveats:
    * Requires a tracing-enabled RPC (Erigon/OpenEthereum-compatible).
      Standard RPC providers without a trace tier will fail the pre-flight
      check and abort before scanning.
    * The block range is determined once at the start of this function. A
      long-running scan will not update its cutoff as time passes; txs that
      land AFTER the function started are NOT counted.
    * Failed `trace_filter` chunks (timeouts, oversized responses) are
      logged and skipped. A candidate whose txs land mostly inside a
      failed chunk will have an undercount. Lower --chunk-size if you see
      frequent failures.
    * `toAddress=[all candidates]` is sent in a single trace_filter call
      per chunk. Providers have varying limits; if you have thousands of
      candidates, consider post-filtering some out before reaching this
      step (e.g., with --sources bytecode).
    """
    if not contracts:
        return set()

    session = make_session("find-address-refs/1.0")

    print("checking trace_filter support...", flush=True)
    probe = next(iter(contracts)).lower()
    try:
        assert_trace_filter_supported(session, rpc_url, probe, timeout)
    except RpcError as e:
        sys.exit(
            f"\n{e}\n\nThe activity filter needs an Erigon/OpenEthereum-style "
            "tracing RPC (e.g. Chainstack/QuickNode/Alchemy with trace support).\n"
        )

    # resolve_window binary-searches the block timestamp to land precisely
    # on the first block at/after `now - window_days`. The `avg_block_time`
    # argument is only a hint for the initial estimate; the binary search
    # converges regardless of whether the hint was accurate.
    start_block, end_block = resolve_window(
        session, rpc_url, window_days, avg_block_time, timeout,
    )
    print(
        f"activity window: blocks {start_block}..{end_block} "
        f"(~{window_days} days, threshold {min_txs} txs, ext+internal)",
        flush=True,
    )

    def on_chunk(i: int, total: int, a: int, b: int, running: int, failed: int) -> None:
        print(
            f"  chunk {i}/{total} (blocks {a}-{b}) done, "
            f"{running} txs so far, {failed} failed",
            flush=True,
        )

    tx_map, failed = count_unique_txs_per_target(
        session, rpc_url,
        targets=contracts,
        from_block=start_block,
        to_block=end_block,
        chunk_size=chunk_size,
        timeout=timeout,
        workers=trace_workers,
        on_chunk=on_chunk,
    )

    passing = {c for c, txs in tx_map.items() if len(txs) >= min_txs}
    print(
        f"activity filter: {len(passing)}/{len(contracts)} contracts pass "
        f"(failed chunks: {failed})"
    )
    return passing


# ---------------------------------------------------------------------------
# Storage verification
# ---------------------------------------------------------------------------

def rpc_get_storage_at(
    session, rpc_url: str, contract: str, slot: int,
) -> str:
    """
    Wrapper around eth_getStorageAt returning a lowercase hex string.

    Reads at block "latest" — this is always CURRENT state, not historical.
    A contract that once stored the target and later cleared it returns
    zeroes here.
    """
    val = rpc_post(
        session, rpc_url, "eth_getStorageAt",
        [contract, hex(slot), "latest"], 15,
    )
    if not isinstance(val, str):
        raise RuntimeError(f"eth_getStorageAt returned {type(val).__name__}")
    return val.lower()


def verify_storage(
    rpc_url: str,
    contracts: list[str],
    target_raw: str,
    slots_to_scan: int = 50,
    workers: int = 8,
) -> dict[str, list[int]]:
    """
    For each contract, read slots [0, slots_to_scan) and check whether the
    target's raw 20-byte hex appears anywhere inside each 32-byte slot value.

    Returns {contract_lowercase -> [slot indices where the pattern matched]}.

    Limitations:
    * Only scans the LINEAR slot range 0..slots_to_scan-1. Misses:
        - Addresses in mappings (slot = keccak256(key . baseSlot))
        - Addresses in structs/arrays past slots_to_scan-1
        - Addresses at EIP-1967 proxy slots (very high hashed slots)
      Callers should be aware that a negative result ≠ "address not
      stored"; it means "not found in the first N linear slots".
    * Uses "latest" — current state only.
    * Substring match within the 32-byte slot value, so:
        - Correctly catches packed-slot addresses.
        - May report a slot index that's shared with other packed fields;
          the slot contains the address but may not be "conceptually" an
          address-typed variable.
    * Total RPC calls = len(contracts) × slots_to_scan. On a free tier
      RPC this can hit rate limits for large candidate lists.
    """
    target_hex = target_raw[2:]
    session = make_session("find-address-refs/1.0")

    def scan(contract: str) -> tuple[str, list[int]]:
        matched: list[int] = []
        for slot in range(slots_to_scan):
            try:
                val = rpc_get_storage_at(session, rpc_url, contract, slot)
            except Exception as e:
                print(f"  [{contract}] slot {slot} error: {e}", file=sys.stderr)
                continue
            if target_hex in val[2:]:
                matched.append(slot)
        return contract, matched

    results: dict[str, list[int]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(scan, c) for c in contracts]
        for fut in concurrent.futures.as_completed(futures):
            contract, matched = fut.result()
            if matched:
                results[contract] = matched
                print(f"  [{contract}] storage hit at slots {matched}", flush=True)
    return results


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def _format_storage_cell(
    contract: str,
    storage_matches: dict[str, list[int]],
    bytecode_only: set[str],
    verified_targets: set[str],
) -> str:
    """
    Render the storage_slots_matched CSV cell.

    Preference order:
      1. 'exempt'     — bytecode-only contract, skipped by design
      2. 'N,N,...'    — storage slots where the address was found
      3. 'unchecked'  — not sent to verify (only possible with --verify-top)
      4. ''           — should not appear, means filtered out earlier
    """
    if contract in bytecode_only:
        return "exempt"
    slots = storage_matches.get(contract)
    if slots:
        return ",".join(map(str, slots))
    if contract not in verified_targets:
        return "unchecked"
    return ""


def write_per_source_csv(
    out_dir: Path,
    source: str,
    hits: list[Hit],
    kept_contracts: set[str] | None,
    etherscan_links: bool,
    storage_matches: dict[str, list[int]] | None,
    bytecode_only: set[str] | None = None,
    verified_targets: set[str] | None = None,
) -> Path:
    path = out_dir / f"results_{source}.csv"
    headers = ["source", "contract_address", "tx_hash"]
    if etherscan_links:
        headers += ["etherscan_contract", "etherscan_tx"]
    if storage_matches is not None:
        headers += ["storage_slots_matched"]

    bytecode_only = bytecode_only or set()
    verified_targets = verified_targets or set()

    kept = 0
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for h in hits:
            if kept_contracts is not None and h.contract_address not in kept_contracts:
                continue
            row = [h.source, h.contract_address, h.tx_hash]
            if etherscan_links:
                row += [etherscan_addr_url(h.contract_address), etherscan_tx_url(h.tx_hash)]
            if storage_matches is not None:
                row += [_format_storage_cell(
                    h.contract_address, storage_matches, bytecode_only, verified_targets,
                )]
            w.writerow(row)
            kept += 1
    print(f"wrote {kept} rows to {path.name}")
    return path


def write_merged_csv(
    out_dir: Path,
    by_source: dict[str, list[Hit]],
    kept_contracts: set[str] | None,
    etherscan_links: bool,
    storage_matches: dict[str, list[int]] | None,
    bytecode_only: set[str] | None = None,
    verified_targets: set[str] | None = None,
) -> Path:
    agg: dict[str, dict[str, str]] = {}
    source_set: dict[str, set[str]] = {}
    for source, hits in by_source.items():
        for h in hits:
            if kept_contracts is not None and h.contract_address not in kept_contracts:
                continue
            agg.setdefault(h.contract_address, {}).setdefault(source, h.tx_hash)
            source_set.setdefault(h.contract_address, set()).add(source)

    path = out_dir / "results_merged.csv"
    headers = ["contract_address", "sources", "source_count"]
    for s in ALL_SOURCES:
        if s in by_source:
            headers.append(f"tx_{s}")
    if etherscan_links:
        headers.append("etherscan_contract")
    if storage_matches is not None:
        headers.append("storage_slots_matched")

    bytecode_only = bytecode_only or set()
    verified_targets = verified_targets or set()

    addrs = sorted(agg.keys(), key=lambda a: (-len(source_set[a]), a))

    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for a in addrs:
            sources = sorted(source_set[a])
            row = [a, "|".join(sources), len(sources)]
            for s in ALL_SOURCES:
                if s in by_source:
                    row.append(agg[a].get(s, ""))
            if etherscan_links:
                row.append(etherscan_addr_url(a))
            if storage_matches is not None:
                row.append(_format_storage_cell(
                    a, storage_matches, bytecode_only, verified_targets,
                ))
            w.writerow(row)
    print(f"wrote {len(addrs)} merged rows to {path.name}")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_sources(s: str) -> list[str]:
    if s.lower() == "all":
        return list(ALL_SOURCES)
    parts = [p.strip() for p in s.split(",") if p.strip()]
    for p in parts:
        if p not in ALL_SOURCES:
            sys.exit(f"unknown source {p!r}, valid: {ALL_SOURCES}")
    return parts


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("address", help="target address (0x...)")

    p.add_argument(
        "--sources", default="all",
        help=f"comma-separated subset of {ALL_SOURCES}, or 'all' (default)",
    )

    # query IDs default to DEFAULT_QUERY_IDS at the top of the file;
    # CLI flags override per-invocation
    p.add_argument("--query-bytecode", type=int, default=DEFAULT_QUERY_IDS["bytecode"])
    p.add_argument("--query-tx",       type=int, default=DEFAULT_QUERY_IDS["tx_input"])
    p.add_argument("--query-trace",    type=int, default=DEFAULT_QUERY_IDS["trace_input"])
    p.add_argument("--query-log",      type=int, default=DEFAULT_QUERY_IDS["event_log"])

    p.add_argument("--out-dir", default=".", help="directory to write CSVs into")

    p.add_argument("--etherscan-links", action="store_true",
                   help="add etherscan URL columns to all CSVs")

    # activity filter (via tracing RPC — counts external + internal txs)
    p.add_argument("--min-txs", type=int, default=0,
                   help="minimum external+internal tx count in window; 0 disables filter")
    p.add_argument("--window-days", type=int, default=30,
                   help="activity window in days (default 30)")
    p.add_argument("--chunk-size", type=int, default=1000,
                   help="blocks per trace_filter chunk (default 1000)")
    p.add_argument("--trace-workers", type=int, default=1,
                   help="parallel trace_filter chunks (default 1; raise for faster, "
                        "compatible providers — lower if you hit rate limits)")
    p.add_argument("--avg-block-time", type=int, default=12,
                   help="average block time in seconds, used for initial range estimate (default 12)")
    p.add_argument("--trace-timeout", type=int, default=60,
                   help="per-RPC-call timeout in seconds (default 60)")

    p.add_argument("--dune-api-key", default=os.environ.get("DUNE_API_KEY"),
                   help="Dune Analytics API key (or set DUNE_API_KEY env var)")
    p.add_argument("--rpc-url", default=os.environ.get("ETH_RPC_URL", ""),
                   help="Ethereum RPC URL. Needs standard RPC for --verify and "
                        "a TRACING-enabled RPC for --min-txs (or set ETH_RPC_URL env var)")

    # storage verify — on by default; use --no-verify to skip.
    # Contracts that come ONLY from the 'bytecode' source are exempt from the
    # storage check, since addresses baked in as `constant` or `immutable` live
    # in runtime bytecode rather than storage and would always fail this check.
    p.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True,
                   help="read storage via RPC to confirm candidates (default on; "
                        "use --no-verify to skip)")
    p.add_argument("--verify-slots", type=int, default=50)
    p.add_argument("--verify-top",   type=int, default=0,
                   help="verify only top N candidates (0 = all passing filter)")

    args = p.parse_args()

    sources = parse_sources(args.sources)
    id_map = {
        "bytecode":    args.query_bytecode,
        "tx_input":    args.query_tx,
        "trace_input": args.query_trace,
        "event_log":   args.query_log,
    }
    query_ids = {s: id_map[s] for s in sources if id_map[s] > 0}
    missing = [s for s in sources if id_map[s] <= 0]
    if missing:
        sys.exit(f"missing --query-* ids for sources: {missing}")

    dune_key = args.dune_api_key
    if not dune_key:
        sys.exit("--dune-api-key is required (or set DUNE_API_KEY env var)")

    rpc_url = args.rpc_url
    need_rpc = args.verify or args.min_txs > 0
    if need_rpc and not rpc_url:
        sys.exit(
            "--rpc-url is required when --verify is on or --min-txs > 0 "
            "(or set ETH_RPC_URL env var). --verify needs any RPC; the "
            "activity filter needs a TRACING RPC (Erigon/OpenEthereum "
            "compatible, e.g. Chainstack/QuickNode/Alchemy)."
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    raw, padded = normalize_addr(args.address)
    print(f"target address: {raw}")
    print(f"running sources: {list(query_ids.keys())}\n")

    # -----------------------------------------------------------------
    # STAGE 1: Dune queries (parallel, one per selected source)
    # -----------------------------------------------------------------
    # Each source uses a DIFFERENT Dune table and catches a different kind
    # of reference — see module-level docstring for details. Queries 2, 3,
    # and 4 are time-bounded to the last 365 days inside the SQL to keep
    # Dune scan cost manageable. The bytecode query is UNBOUNDED, so old
    # deploys with hardcoded references are still caught. ERC-20/721/1155
    # standard selectors and topic0s are excluded at the SQL level.
    by_source = run_queries(dune_key, query_ids, raw, padded)
    total = sum(len(h) for h in by_source.values())
    unique = {h.contract_address for hits in by_source.values() for h in hits}
    print(f"\ntotal raw hits: {total}, unique contracts: {len(unique)}\n")

    # -----------------------------------------------------------------
    # STAGE 2: activity filter (opt-in via --min-txs)
    # -----------------------------------------------------------------
    # Drops candidates with fewer than --min-txs unique transactions in
    # the last --window-days days. Counts BOTH direct (external) and
    # internal calls uniformly via trace_filter — a single trace frame
    # reaching the contract contributes its tx hash, which is deduped
    # per contract. Requires a tracing-enabled RPC.
    kept_contracts: set[str] | None = None
    if args.min_txs > 0 and unique:
        kept_contracts = filter_by_activity(
            sorted(unique),
            rpc_url,
            args.min_txs,
            args.window_days,
            chunk_size=args.chunk_size,
            timeout=args.trace_timeout,
            avg_block_time=args.avg_block_time,
            trace_workers=args.trace_workers,
        )
        print()

    # -----------------------------------------------------------------
    # STAGE 3: storage verification (default ON — see --no-verify to skip)
    # -----------------------------------------------------------------
    # The verify stage reads the first N storage slots of each candidate
    # and keeps only those where the target's 20-byte pattern appears.
    # This drops historical references that were later overwritten, and
    # removes raw-bytecode false positives where the address appears in
    # creation code but was never stored in state.
    #
    # CAVEAT: The linear slot scan cannot see mapping entries, deep
    # struct fields, or EIP-1967 proxy slots. If your target is expected
    # to live in any of those, run with --no-verify. See module-level
    # docstring ("WHAT IT MISSES") for the full list.
    #
    # BYTECODE-ONLY EXEMPTION: contracts whose ONLY Dune signal was the
    # bytecode source are skipped here — the address they match is
    # typically a constant/immutable in runtime bytecode, not storage.
    # They're kept in the output marked as `exempt`. See module-level
    # docstring ("BYTECODE-ONLY EXEMPTION") for the rationale.
    storage_matches: dict[str, list[int]] | None = None
    verified_targets: set[str] = set()
    bytecode_only: set[str] = set()

    if args.verify:
        # Build a per-contract source set to detect bytecode-only candidates.
        # A contract is bytecode-only iff its source set == {"bytecode"}.
        # Multi-source contracts (bytecode + any runtime source) are NOT
        # bytecode-only and get verified normally — this is correct because
        # a runtime signal (tx/trace/event) typically means the address was
        # actually written to state at some point.
        source_map: dict[str, set[str]] = {}
        for src, hits in by_source.items():
            for h in hits:
                source_map.setdefault(h.contract_address, set()).add(src)
        bytecode_only = {c for c, srcs in source_map.items() if srcs == {"bytecode"}}

        pool = sorted(kept_contracts) if kept_contracts is not None else sorted(unique)
        verify_list = [c for c in pool if c not in bytecode_only]
        if args.verify_top > 0:
            # Truncating the verify list means contracts past the cutoff
            # are marked `unchecked` (not `no_match`), i.e. we cannot make
            # a positive or negative claim about them. They're kept in the
            # output regardless.
            verify_list = verify_list[:args.verify_top]
        verified_targets = set(verify_list)

        pool_bytecode_only = bytecode_only.intersection(pool)
        print(
            f"verifying storage on {len(verify_list)} contracts "
            f"({len(pool_bytecode_only)} exempt as bytecode-only, "
            f"slots 0..{args.verify_slots - 1})..."
        )
        storage_matches = (
            verify_storage(rpc_url, verify_list, raw, args.verify_slots)
            if verify_list else {}
        )

        # Final filter composition:
        #   kept = (in bytecode_only)      -> exempt, unconditionally kept
        #        ∪ (in storage_matches)    -> storage-confirmed, kept
        #        ∪ (not in verified_targets) -> unchecked due to verify-top,
        #                                       kept since we have no signal
        # Contracts that WERE checked and returned no matches are the ones
        # silently removed here — that's the whole point of the stage.
        base = set(pool)
        kept_contracts = {
            c for c in base
            if c in bytecode_only
            or c in storage_matches
            or c not in verified_targets
        }
        print(
            f"after storage verify: {len(kept_contracts)} contracts remain "
            f"({len(kept_contracts & bytecode_only)} exempt, "
            f"{len(storage_matches)} with storage match, "
            f"{len(kept_contracts - bytecode_only - set(storage_matches))} unchecked)"
        )
        print()

    # -----------------------------------------------------------------
    # STAGE 4: write outputs
    # -----------------------------------------------------------------
    # One CSV per source (named results_<source>.csv), plus a merged
    # results_merged.csv if more than one source ran. The merged file
    # aggregates by contract_address and ranks contracts by source count
    # (how many independent signals they triggered).
    #
    # The `storage_slots_matched` column is present only if --verify was
    # on. Values:
    #   "exempt"     : bytecode-only, not checked
    #   "3,7"        : checked and found at slots 3 and 7
    #   "unchecked"  : was outside --verify-top, not checked (no claim made)
    print("writing CSVs:")
    for source, hits in by_source.items():
        write_per_source_csv(
            out_dir, source, hits, kept_contracts,
            args.etherscan_links, storage_matches,
            bytecode_only, verified_targets,
        )
    if len(by_source) > 1:
        write_merged_csv(
            out_dir, by_source, kept_contracts,
            args.etherscan_links, storage_matches,
            bytecode_only, verified_targets,
        )


if __name__ == "__main__":
    main()
