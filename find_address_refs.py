"""
Find contracts on Ethereum mainnet that reference a specific target address.

Runs a three-stage pipeline:
  1. Discovery via four parallel Dune queries (bytecode, tx input, internal
     traces, event logs), tagged by source.
  2. Optional activity filter via trace_filter on a tracing-enabled RPC.
  3. Storage verification (default on) via eth_getStorageAt.

Output is one CSV per source plus a merged CSV when multiple sources run.

See README.md for the full pipeline description, known limitations (time
bounds, mappings, proxies, ERC-20 filtering), required environment, and
the bytecode-only exemption rationale.
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

# Saved Dune query IDs — edit these after creating the queries, or pass
# them per-invocation via --query-* CLI flags. Set to 0 for any source
# you haven't saved; missing IDs only error out for sources you select.
DEFAULT_QUERY_IDS: dict[str, int] = {
    "bytecode":    7349915,
    "tx_input":    7349946,
    "trace_input": 7349959,
    "event_log":   7349970,
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

# Which parameter(s) each saved query declares on Dune. We send only
# these per query — Dune's API rejects extras with HTTP 400.
QUERY_PARAMS: dict[str, tuple[str, ...]] = {
    "bytecode":    ("target_address_raw",),
    "tx_input":    ("target_address_padded",),
    "trace_input": ("target_address_padded",),
    "event_log":   ("target_address_padded",),
}


_HEX_CHARS = set("0123456789abcdef")


def _clean_hex_address(raw: object) -> str | None:
    """
    Normalize an address value coming out of Dune into `0x<40 lowercase hex>`.
    Returns None if the value can't be coerced into a valid address —
    callers should filter these out before sending to downstream RPCs.
    Dune occasionally emits bytea as \\x... or with padding/whitespace that
    blows up RPC JSON parsers.
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s.startswith("\\x"):      # Dune bytea form
        s = "0x" + s[2:]
    if not s.startswith("0x"):
        s = "0x" + s
    body = s[2:]
    if len(body) != 40 or not all(c in _HEX_CHARS for c in body):
        return None
    return s


def run_queries(
    dune_key: str,
    query_ids: dict[str, int],
    target_raw: str,
    target_padded: str,
) -> dict[str, list[Hit]]:
    """Runs selected queries in parallel. Returns source -> list of Hits."""
    all_params = {"target_address_raw": target_raw, "target_address_padded": target_padded}
    dune = DuneClient(dune_key)

    def run_one(source: str, qid: int) -> tuple[str, list[Hit]]:
        print(f"[{source}] executing query {qid}...", flush=True)
        t0 = time.time()
        params = {k: all_params[k] for k in QUERY_PARAMS.get(source, all_params.keys())}
        rows = dune.run(qid, params)
        print(f"[{source}] {len(rows)} rows in {time.time() - t0:.1f}s", flush=True)
        hits: list[Hit] = []
        dropped = 0
        for r in rows:
            addr = _clean_hex_address(r.get("contract_address"))
            txh = str(r.get("tx_hash") or "").strip().lower()
            if addr is None or not txh:
                dropped += 1
                continue
            hits.append(Hit(source=source, contract_address=addr, tx_hash=txh))
        if dropped:
            print(f"[{source}] dropped {dropped} row(s) with malformed address/tx_hash",
                  file=sys.stderr)
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
    Return the subset of `contracts` that received >= `min_txs` unique
    transactions in the last `window_days`, counting direct and internal
    calls uniformly.

    Uses trace_filter (requires a tracing-enabled RPC). Counts UNIQUE tx
    hashes per contract — a tx that hits the contract 100 times via
    internal calls counts as one.
    """
    if not contracts:
        return set()

    # Defense-in-depth: normalize + dedup before sending to trace_filter.
    # Chainstack (and other providers) reject the entire batch with
    # "invalid string length" if ANY address in the toAddress array is
    # malformed, so one bad entry poisons every chunk.
    clean: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for c in contracts:
        a = _clean_hex_address(c)
        if a is None:
            dropped += 1
            continue
        if a in seen:
            continue
        seen.add(a)
        clean.append(a)
    if dropped:
        print(f"filter_by_activity: dropped {dropped} malformed address(es) "
              f"before trace_filter", file=sys.stderr)
    if not clean:
        return set()
    contracts = clean

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
    """Read a storage slot at block 'latest' and return it as lowercase hex."""
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
    For each contract, read slots [0, slots_to_scan) and return
    {contract -> [slots where the 20-byte target appears in the 32-byte value]}.

    Contracts with no matches are OMITTED from the returned dict. Only
    scans the linear slot range — mapping entries and far-offset structs
    require adjusting `slots_to_scan` or a different approach (see README).
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
    Render the storage_slots_matched CSV cell. Precedence:
      'exempt'    if bytecode-only
      'N,N,...'   if slots matched
      'unchecked' if not sent to verify (--verify-top truncation)
      ''          otherwise (shouldn't appear — would indicate a filter bug)
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

    # --- Stage 1: Dune discovery (parallel, one call per source) ---
    by_source = run_queries(dune_key, query_ids, raw, padded)
    total = sum(len(h) for h in by_source.values())
    unique = {h.contract_address for hits in by_source.values() for h in hits}
    print(f"\ntotal raw hits: {total}, unique contracts: {len(unique)}\n")

    # --- Stage 2: activity filter (optional, via --min-txs) ---
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

    # --- Stage 3: storage verification (default on, --no-verify to skip) ---
    storage_matches: dict[str, list[int]] | None = None
    verified_targets: set[str] = set()
    bytecode_only: set[str] = set()

    if args.verify:
        # Bytecode-only candidates (sole signal = bytecode source) are exempt
        # from the storage check. Rationale: `address constant` and `immutable`
        # declarations are patched into RUNTIME BYTECODE at deploy time, not
        # storage, so eth_getStorageAt would always miss them and the default
        # filter would drop correctly-identified contracts. A contract that
        # shows up in bytecode AND any runtime source is NOT bytecode-only
        # and gets verified normally.
        source_map: dict[str, set[str]] = {}
        for src, hits in by_source.items():
            for h in hits:
                source_map.setdefault(h.contract_address, set()).add(src)
        bytecode_only = {c for c, srcs in source_map.items() if srcs == {"bytecode"}}

        pool = sorted(kept_contracts) if kept_contracts is not None else sorted(unique)
        verify_list = [c for c in pool if c not in bytecode_only]
        if args.verify_top > 0:
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

        # Final kept set: exempt ∪ matched ∪ unchecked (outside --verify-top).
        # Contracts that WERE checked and had no match are the ones dropped.
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

    # --- Stage 4: write CSVs (one per source, plus merged if >1 source) ---
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
    