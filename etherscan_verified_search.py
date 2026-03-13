#!/usr/bin/env python3
"""
Search verified contracts via Etherscan Smart Contract Search, filter by recent activity,
and report line-level matches of the search query in verified source code.

What it does:
- Scrapes Etherscan Smart Contract Search pages as discovery
- Collects candidate verified contract addresses
- Fetches verified source via Etherscan V2 getsourcecode
- Locates every occurrence of the search query in source, with filename and line number
- Filters out contracts with fewer than X call traces in the last Y months
- Prints matching contracts or writes CSV

Tx counting via trace_filter (RPC):
    Unlike Etherscan's txlist/txlistinternal APIs, trace_filter returns ALL
    incoming call traces including zero-value delegatecalls.  This means
    contracts deployed behind a proxy (which receive traffic exclusively as
    zero-value delegatecalls) are counted correctly and not filtered out.
    Requires a Chainstack archive node (or any node that exposes trace_filter).

Requirements:
    pip install requests beautifulsoup4 python-dateutil tqdm

Examples:
    python etherscan_smart_search_filter.py \\
      --apikey YOUR_KEY \\
      --rpc-url https://YOUR_CHAINSTACK_ENDPOINT \\
      --query "finalizeEthWithdrawal" \\
      --min-txs 10 \\
      --months 6

    python etherscan_smart_search_filter.py \\
      --apikey YOUR_KEY \\
      --rpc-url https://YOUR_CHAINSTACK_ENDPOINT \\
      --query "requestL2Transaction" \\
      --min-txs 5 \\
      --months 3 \\
      --output csv \\
      --csv-path matches.csv

Main limitation:
    The discovery step scrapes the Etherscan frontend, because there is no
    official documented API endpoint for Smart Contract Search results.
    If Etherscan changes website parameters or HTML structure, the discovery
    logic will need updating.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import requests
from bs4 import BeautifulSoup
from dateutil.relativedelta import relativedelta
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


ETHERSCAN_WEB_BASE = "https://etherscan.io"
ETHERSCAN_API_BASE = "https://api.etherscan.io/v2/api"
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")

# ---------------------------------------------------------------------------
# Token-bucket rate limiter (shared across all Etherscan API calls)
# ---------------------------------------------------------------------------

class RateLimiter:
    """
    Simple token-bucket limiter. 2.5 calls/sec stays safely under
    Etherscan's free-tier hard limit of 3/sec.
    """
    def __init__(self, calls_per_second: float = 2.5):
        self._interval = 1.0 / calls_per_second
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            gap = self._interval - (now - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


_rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class EtherscanError(RuntimeError):
    pass


@dataclass
class MatchLocation:
    """A single occurrence of the query string in a source file."""
    filename: str
    line: int

    def __str__(self) -> str:
        return f"{self.filename}#{self.line}"


@dataclass
class MatchRow:
    address: str
    contract_name: str
    compiler_version: str
    tx_count_recent: int
    match_locations: List[MatchLocation]
    discovery_url: str


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Search Etherscan Smart Contract Search results, "
            "then filter by recent tx count and report source line matches."
        )
    )
    p.add_argument("--apikey", required=True, help="Etherscan API key")
    p.add_argument(
        "--query", required=True,
        help="Keyword/function name to search in Smart Contract Search and locate in source",
    )
    p.add_argument("--min-txs", type=int, required=True,
                   help="Minimum call traces (direct + delegatecall) in the lookback window")
    p.add_argument("--months", type=int, required=True,
                   help="Lookback window in months")
    p.add_argument(
        "--rpc-url", default=None,
        help=(
            "Ethereum JSON-RPC endpoint that supports trace_filter "
            "(e.g. a Chainstack archive node). Required when --min-txs > 0. "
            "trace_filter counts all incoming call traces including zero-value "
            "delegatecalls, so contracts behind proxies are counted correctly."
        ),
    )
    p.add_argument("--chainid", default="1",
                   help="Etherscan V2 chainid (default: 1)")
    p.add_argument("--max-pages", type=int, default=10,
                   help="Max Smart Contract Search result pages to crawl")
    p.add_argument("--timeout", type=int, default=30,
                   help="HTTP timeout in seconds")
    p.add_argument("--page-delay", type=float, default=0.5,
                   help="Delay between search page fetches (seconds)")
    p.add_argument("--case-sensitive", action="store_true",
                   help="Case-sensitive query matching in source code")
    p.add_argument("--stop-after", type=int, default=0,
                   help="Stop after discovering this many addresses (0 = unlimited)")
    p.add_argument("--output", choices=["text", "csv"], default="text",
                   help="Output format")
    p.add_argument("--csv-path", default="matches.csv",
                   help="CSV output path")

    return p


# ---------------------------------------------------------------------------
# Etherscan API helpers
# ---------------------------------------------------------------------------

def etherscan_get(
    session: requests.Session,
    params: Dict[str, str],
    timeout: int,
    max_retries: int = 4,
) -> dict:
    """
    GET the Etherscan V2 API with retry on:
      - HTTP 403 / 429 (rate limit)
      - HTTP 5xx (server errors)
      - JSON result containing 'rate limit' (HTTP 200 but soft-limited)
    Uses exponential back-off. Raises EtherscanError on persistent failure
    or a definitive API-level error.
    """
    retryable = {403, 429, 500, 502, 503, 504}

    for attempt in range(max_retries):
        _rate_limiter.wait()
        resp = session.get(ETHERSCAN_API_BASE, params=params, timeout=timeout)

        if resp.status_code in retryable:
            wait = 2 ** attempt
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = max(wait, int(retry_after))
                except ValueError:
                    pass
            if resp.status_code == 403:
                reset_ts = resp.headers.get("X-RateLimit-Reset")
                if reset_ts:
                    try:
                        wait = max(wait, int(reset_ts) - int(time.time()) + 1)
                    except ValueError:
                        pass
            print(
                f"  [HTTP {resp.status_code}] retrying in {wait}s "
                f"(attempt {attempt + 1}/{max_retries})",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        resp.raise_for_status()
        data = resp.json()

        status = data.get("status")
        result = data.get("result")
        message = data.get("message", "")

        if status == "1":
            return data

        # "No transactions found" is a valid empty result, not an error
        if isinstance(result, str) and "no " in result.lower():
            return data

        # Rate limit returned as HTTP 200 with status="0" — treat as retryable
        if isinstance(result, str) and "rate limit" in result.lower():
            wait = 2 ** attempt
            print(
                f"  [rate limit] retrying in {wait}s "
                f"(attempt {attempt + 1}/{max_retries}): {result}",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        raise EtherscanError(
            f"Etherscan API error: message={message!r}, result={result!r}"
        )

    raise EtherscanError(
        f"Etherscan API call failed after {max_retries} attempts. "
        f"Last params: {params}"
    )


def get_source_code(
    session: requests.Session,
    address: str,
    apikey: str,
    chainid: str,
    timeout: int,
) -> dict:
    """
    Fetch verified source code metadata for a contract.
    Returns the first result dict from getsourcecode, or raises EtherscanError.
    """
    data = etherscan_get(
        session,
        {
            "chainid": chainid,
            "module": "contract",
            "action": "getsourcecode",
            "address": address,
            "apikey": apikey,
        },
        timeout,
    )
    results = data.get("result", [])
    if not results or not isinstance(results, list):
        raise EtherscanError(f"No source result for {address}")
    return results[0]


def get_block_by_timestamp(
    session: requests.Session,
    timestamp: int,
    apikey: str,
    chainid: str,
    timeout: int,
) -> Optional[int]:
    """
    Return the closest block number at or after `timestamp`.
    Returns None if the call fails so callers can fall back gracefully.
    """
    try:
        data = etherscan_get(
            session,
            {
                "chainid": chainid,
                "module": "block",
                "action": "getblocknobytime",
                "timestamp": str(timestamp),
                "closest": "after",
                "apikey": apikey,
            },
            timeout,
        )
        result = data.get("result")
        if result and str(result).isdigit():
            return int(result)
    except EtherscanError:
        pass
    return None


# ---------------------------------------------------------------------------
# Source code parsing — find query occurrences with filename + line number
# ---------------------------------------------------------------------------

def find_query_in_source(
    source_code_field: str,
    query: str,
    case_sensitive: bool,
) -> List[MatchLocation]:
    """
    Parse the SourceCode field (raw Solidity, standard-JSON, or multi-file)
    and return every occurrence of `query` as a (filename, line_number) pair.

    Handles three Etherscan formats:
      1. Raw Solidity string
      2. Standard-JSON wrapped in double braces: {{ ... }}
      3. Single-file JSON with a top-level "SourceCode" key (rare)
    """
    if not source_code_field:
        return []

    needle = query if case_sensitive else query.lower()
    locations: List[MatchLocation] = []

    def scan_text(text: str, filename: str) -> None:
        lines = text.splitlines()
        for lineno, line in enumerate(lines, start=1):
            haystack = line if case_sensitive else line.lower()
            if needle in haystack:
                locations.append(MatchLocation(filename=filename, line=lineno))

    text = source_code_field.strip()

    # Standard-JSON: double-brace wrapped  {{ { "language": "Solidity", "sources": { ... } } }}
    if text.startswith("{{") and text.endswith("}}"):
        try:
            obj = json.loads(text[1:-1])
            sources = obj.get("sources", {})
            if sources:
                for fname, fdata in sources.items():
                    content = fdata.get("content", "")
                    if content:
                        scan_text(content, fname)
                return locations
        except (json.JSONDecodeError, AttributeError):
            pass  # fall through to raw treatment

    # Plain JSON (single-file edge case)
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            content = obj.get("SourceCode") or obj.get("content") or ""
            if content:
                scan_text(content, "contract.sol")
                return locations
        except (json.JSONDecodeError, AttributeError):
            pass

    # Raw Solidity
    scan_text(text, "contract.sol")
    return locations


# ---------------------------------------------------------------------------
# Tx count via trace_filter (JSON-RPC)
# ---------------------------------------------------------------------------

def _trace_filter_one_chunk(
    rpc_url: str,
    address: str,
    from_block: int,
    to_block: int,
    timeout: int,
    max_retries: int,
) -> int:
    """
    Run trace_filter for a single block range chunk and return the trace count.
    Paginates within the chunk using after/count.
    """
    page_size = 200
    after = 0
    count = 0

    from_block_hex = hex(from_block)
    to_block_hex   = hex(to_block)

    while True:
        payload = {
            "jsonrpc": "2.0",
            "method": "trace_filter",
            "params": [{
                "toAddress": [address],
                "fromBlock": from_block_hex,
                "toBlock":   to_block_hex,
                "after":     after,
                "count":     page_size,
            }],
            "id": 1,
        }

        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                resp = requests.post(rpc_url, json=payload, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:
                last_exc = exc
                wait = 2 ** attempt
                time.sleep(wait)
        else:
            raise RuntimeError(
                f"trace_filter failed after {max_retries} attempts for {address}: {last_exc}"
            )

        error = data.get("error")
        if error:
            raise RuntimeError(
                f"trace_filter RPC error for {address}: {error.get('message', error)}"
            )

        traces = data.get("result") or []
        count += len(traces)

        if len(traces) < page_size:
            break

        after += page_size

    return count



def fetch_trace_tx_count(
    rpc_url: str,
    address: str,
    from_block: int,
    to_block: int,
    timeout: int,
    max_retries: int = 4,
    block_chunk_size: int = 1_000,
    min_txs: int = 0,
) -> int:
    """
    Count all incoming call traces to `address` between `from_block` and
    `to_block` using the trace_filter JSON-RPC method.

    trace_filter returns every call trace regardless of ETH value, including
    zero-value delegatecalls. This means contracts deployed behind a proxy
    (which receive no direct txs and no ETH-value internal calls, only
    zero-value delegatecalls) are counted correctly.

    Requires an archive node that exposes the trace_filter method
    (e.g. Chainstack archive nodes, Erigon, OpenEthereum).

    Splits the full block range into chunks of `block_chunk_size` to comply
    with nodes that impose a per-call block range limit.

    If `min_txs` > 0, stops as soon as the running count reaches that
    threshold — there is no point scanning further once the filter is satisfied.
    Returns the (partial) count in that case, which is >= min_txs.
    """
    total_blocks = to_block - from_block + 1
    n_chunks = (total_blocks + block_chunk_size - 1) // block_chunk_size

    count = 0
    chunk_start = from_block

    # Inner progress bar so per-contract scanning is visible
    chunk_iter = range(n_chunks)
    if tqdm and n_chunks > 1:
        chunk_iter = tqdm(
            chunk_iter,
            desc=f"  {address[:10]}…",
            unit="chunk",
            leave=False,
        )

    for _ in chunk_iter:
        chunk_end = min(chunk_start + block_chunk_size - 1, to_block)
        count += _trace_filter_one_chunk(
            rpc_url=rpc_url,
            address=address,
            from_block=chunk_start,
            to_block=chunk_end,
            timeout=timeout,
            max_retries=max_retries,
        )
        chunk_start = chunk_end + 1

        # Early exit: already enough traces to pass the filter
        if min_txs > 0 and count >= min_txs:
            if tqdm and hasattr(chunk_iter, "close"):
                chunk_iter.close()
            break

    return count


def get_block_by_timestamp_rpc(
    rpc_url: str,
    timestamp: int,
    timeout: int,
) -> Optional[int]:
    """
    Binary-search for the block number closest to `timestamp` using
    eth_getBlockByNumber via JSON-RPC.

    Used as a fallback / complement to Etherscan's getblocknobytime so that
    the from_block for trace_filter can be determined without an extra
    Etherscan API call when rpc_url is available.
    """
    try:
        # Get latest block number
        resp = requests.post(
            rpc_url,
            json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
            timeout=timeout,
        )
        resp.raise_for_status()
        latest = int(resp.json()["result"], 16)

        lo, hi = 0, latest
        while lo < hi:
            mid = (lo + hi) // 2
            resp = requests.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "method": "eth_getBlockByNumber",
                    "params": [hex(mid), False],
                    "id": 1,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            block = resp.json().get("result") or {}
            block_ts = int(block.get("timestamp", "0x0"), 16)

            if block_ts < timestamp:
                lo = mid + 1
            else:
                hi = mid

        return lo
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Discovery (web scraping)
# ---------------------------------------------------------------------------

def extract_addresses_from_html(html: str) -> List[str]:
    """
    Extract contract addresses from Etherscan search result HTML.
    Each result card contains an anchor like:
      <a href="/address/0xABCD...#code">0xABCD...</a>
    """
    addresses = re.findall(r'/address/(0x[a-fA-F0-9]{40})', html)
    out: List[str] = []
    seen: set = set()
    for addr in addresses:
        addr_l = addr.lower()
        if addr_l not in seen:
            seen.add(addr_l)
            out.append(addr)
    return out


def fetch_search_result_page(
    session: requests.Session,
    query: str,
    page_num: int,
    timeout: int,
    page_size: int = 100,
) -> Tuple[str, str]:
    """
    Fetch one page of Etherscan Smart Contract Search results.
    URL: /searchcontractlist?a=all&q=QUERY&ps=PAGE_SIZE&p=PAGE_NUM
    'a=all' is required — without it Etherscan returns no results.
    """
    url = f"{ETHERSCAN_WEB_BASE}/searchcontractlist"
    params: Dict[str, str] = {
        "a": "all",
        "q": query,
        "ps": str(page_size),
        "p": str(page_num),
    }
    resp = session.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 (compatible; etherscan-contract-search-scanner/2.0)"},
    )
    resp.raise_for_status()
    return resp.text, resp.url


def discover_addresses_via_smart_contract_search(
    session: requests.Session,
    query: str,
    max_pages: int,
    timeout: int,
    page_delay: float,
    stop_after: int,
) -> Tuple[List[str], Dict[str, str]]:
    """
    Scrape Etherscan Smart Contract Search results and return discovered addresses.

    Returns:
      - unique addresses in discovery order
      - mapping of address (lowercased) -> discovery page URL
    """
    all_addresses: List[str] = []
    address_to_url: Dict[str, str] = {}
    seen: set = set()

    for page_num in range(1, max_pages + 1):
        if page_num * 100 > 10_000:
            print(
                f"  [warn] stopping discovery at page {page_num}: would exceed "
                f"Etherscan's 10,000-result window limit.",
                file=sys.stderr,
            )
            break

        if page_num > 1:
            time.sleep(page_delay)

        html, final_url = fetch_search_result_page(
            session, query, page_num, timeout, page_size=100
        )

        page_addrs = extract_addresses_from_html(html)

        if not page_addrs:
            if page_num == 1:
                print(
                    f"  [warn] page 1 returned no addresses. "
                    f"Check URL manually: {final_url}",
                    file=sys.stderr,
                )
            break

        for addr in page_addrs:
            addr_l = addr.lower()
            if addr_l not in seen:
                seen.add(addr_l)
                all_addresses.append(addr)
                address_to_url[addr_l] = final_url
                if stop_after and len(all_addresses) >= stop_after:
                    return all_addresses, address_to_url

        print(f"  Page {page_num}: {len(page_addrs)} addresses found (total so far: {len(all_addresses)})", flush=True)

        if len(page_addrs) < 100:
            break

    return all_addresses, address_to_url


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def write_csv(rows: Sequence[MatchRow], path: str, query: str) -> None:
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "address", "contract_name", "compiler_version",
                "tx_count_recent", f"matched:{query}", "discovery_url",
            ])
            for row in rows:
                locs = ", ".join(str(loc) for loc in row.match_locations)
                w.writerow([
                    row.address,
                    row.contract_name,
                    row.compiler_version,
                    row.tx_count_recent,
                    locs,
                    row.discovery_url,
                ])
    except OSError as e:
        print(f"ERROR: could not write CSV to {path!r}: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = build_parser().parse_args()

    if args.months < 0:
        raise SystemExit("--months must be >= 0")
    if args.min_txs < 0:
        raise SystemExit("--min-txs must be >= 0")
    if args.max_pages <= 0:
        raise SystemExit("--max-pages must be > 0")
    if args.min_txs > 0 and not args.rpc_url:
        raise SystemExit(
            "--rpc-url is required when --min-txs > 0. "
            "Provide a Chainstack archive node endpoint that supports trace_filter."
        )

    cutoff_dt = datetime.now(timezone.utc) - relativedelta(months=args.months)
    cutoff_ts = int(cutoff_dt.timestamp())

    session = requests.Session()
    session.headers.update({"User-Agent": "etherscan-contract-search-scanner/2.0"})

    # ---- Discovery ----------------------------------------------------------
    discovered, address_to_url = discover_addresses_via_smart_contract_search(
        session=session,
        query=args.query,
        max_pages=args.max_pages,
        timeout=args.timeout,
        page_delay=args.page_delay,
        stop_after=args.stop_after,
    )

    if not discovered:
        raise SystemExit(
            "No addresses discovered. Either the query returned no results, "
            "or Etherscan has changed its HTML structure.\n"
            "Verify manually: "
            f"https://etherscan.io/searchcontractlist?a=all&q={args.query}&ps=100&p=1"
        )

    print(f"# Smart Contract Search query : {args.query}")
    print(f"# Candidate addresses         : {len(discovered)}")

    # ---- Resolve from_block for trace_filter window -------------------------
    from_block = 0
    if args.months > 0 and args.rpc_url:
        # Try Etherscan first (fast), fall back to RPC binary search
        from_block = get_block_by_timestamp(
            session, cutoff_ts, args.apikey, args.chainid, args.timeout
        ) or get_block_by_timestamp_rpc(
            args.rpc_url, cutoff_ts, args.timeout
        ) or 0
        if from_block:
            print(f"# Trace window from block     : {from_block:,}")
        else:
            print(
                "# [warn] could not resolve start block; scanning full history",
                file=sys.stderr,
            )

    # Get current block for to_block bound
    to_block = 99_999_999
    if args.rpc_url:
        try:
            resp = requests.post(
                args.rpc_url,
                json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
                timeout=args.timeout,
            )
            to_block = int(resp.json()["result"], 16)
            print(f"# Trace window to block       : {to_block:,}")
        except Exception:
            pass

    block_chunk_size = 1_000

    # ---- Per-contract filtering ---------------------------------------------
    matches: List[MatchRow] = []
    scanned = 0
    with_source = 0
    with_query_match = 0
    fallback_url = f"{ETHERSCAN_WEB_BASE}/searchcontract"

    _iter = tqdm(discovered, desc="Scanning contracts", unit="contract") if tqdm else discovered
    for address in _iter:
        scanned += 1
        addr_l = address.lower()
        discovery_url = address_to_url.get(addr_l, fallback_url)

        # Source code
        try:
            source_meta = get_source_code(
                session=session,
                address=address,
                apikey=args.apikey,
                chainid=args.chainid,
                timeout=args.timeout,
            )
        except Exception as e:
            print(f"[warn] source fetch failed for {address}: {e}", file=sys.stderr)
            continue

        source_code = source_meta.get("SourceCode", "") or ""
        contract_name = source_meta.get("ContractName", "") or ""
        compiler_version = source_meta.get("CompilerVersion", "") or ""

        if not source_code:
            continue

        with_source += 1

        # Find query occurrences with filename + line number
        locations = find_query_in_source(source_code, args.query, args.case_sensitive)

        if not locations:
            continue

        with_query_match += 1

        # Tx count via trace_filter — counts all incoming call traces including
        # zero-value delegatecalls, so contracts behind proxies are not penalised.
        if args.rpc_url:
            try:
                recent_tx_count = fetch_trace_tx_count(
                    rpc_url=args.rpc_url,
                    address=address,
                    from_block=from_block,
                    to_block=to_block,
                    timeout=args.timeout,
                    block_chunk_size=block_chunk_size,
                    min_txs=args.min_txs,
                )
            except Exception as e:
                print(f"[warn] trace_filter failed for {address}: {e}", file=sys.stderr)
                continue

            if recent_tx_count < args.min_txs:
                continue
        else:
            # --min-txs 0 with no rpc_url: skip counting entirely
            recent_tx_count = 0

        matches.append(MatchRow(
            address=address,
            contract_name=contract_name,
            compiler_version=compiler_version,
            tx_count_recent=recent_tx_count,
            match_locations=locations,
            discovery_url=discovery_url,
        ))

    matches.sort(key=lambda r: (-r.tx_count_recent, r.address.lower()))

    # ---- Summary ------------------------------------------------------------
    print(f"# Contracts scanned           : {scanned}")
    print(f"# Contracts with source code  : {with_source}")
    print(f"# Source confirms query match : {with_query_match}")
    print(f"# Min txs in last {args.months}m        : {args.min_txs}")
    print(f"# Final matches               : {len(matches)}")
    print()

    if args.output == "csv":
        write_csv(matches, args.csv_path, args.query)
        print(f"Wrote CSV to {args.csv_path}")
        return 0

    for row in matches:
        locs = ", ".join(str(loc) for loc in row.match_locations)
        print(
            f"{row.address} | {row.contract_name or '-'} | "
            f"{row.tx_count_recent} txs/{args.months}m | "
            f"matched: {args.query} [{locs}]"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
