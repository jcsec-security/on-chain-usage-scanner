#!/usr/bin/env python3
"""
Search verified contracts via Etherscan Smart Contract Search, filter by recent activity,
and report line-level matches of the search query in verified source code.

What it does:
- Scrapes Etherscan Smart Contract Search pages as discovery
- Collects candidate verified contract addresses
- Fetches verified source via Etherscan V2 getsourcecode
- Locates every occurrence of the search query in source, with filename and line number
- Filters out contracts with fewer than X normal txs in the last Y months
- Prints matching contracts or writes CSV

Requirements:
    pip install requests beautifulsoup4 python-dateutil tqdm

Examples:
    python etherscan_smart_search_filter.py \\
      --apikey YOUR_KEY \\
      --query "finalizeEthWithdrawal" \\
      --min-txs 10 \\
      --months 6

    python etherscan_smart_search_filter.py \\
      --apikey YOUR_KEY \\
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
# Token-bucket rate limiter (shared across all API calls)
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
                   help="Minimum normal txs in the lookback window")
    p.add_argument("--months", type=int, required=True,
                   help="Lookback window in months")
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
# Tx count
# ---------------------------------------------------------------------------

def fetch_recent_normal_txs_count(
    session: requests.Session,
    address: str,
    apikey: str,
    chainid: str,
    cutoff_ts: int,
    timeout: int,
    start_block: Optional[int],
) -> int:
    """
    Count normal transactions to `address` since `cutoff_ts`.

    Uses `start_block` (derived from getblocknobytime) to avoid paginating
    through the full tx history. Falls back to block 0 if start_block is None.
    Stops before exceeding Etherscan's 10,000-result window cap.
    """
    page = 1
    offset = 1000
    count = 0
    block_param = str(start_block) if start_block is not None else "0"
    max_window = 10_000  # Etherscan hard cap: page × offset <= 10_000

    while True:
        if page * offset > max_window:
            print(
                f"  [warn] txlist for {address}: reached Etherscan 10,000-result "
                f"window limit at page {page}; tx count may be a lower bound.",
                file=sys.stderr,
            )
            break

        try:
            data = etherscan_get(
                session,
                {
                    "chainid": chainid,
                    "module": "account",
                    "action": "txlist",
                    "address": address,
                    "startblock": block_param,
                    "endblock": "9999999999",
                    "page": str(page),
                    "offset": str(offset),
                    "sort": "desc",
                    "apikey": apikey,
                },
                timeout,
            )
        except EtherscanError as exc:
            if "result window is too large" in str(exc).lower():
                print(
                    f"  [warn] txlist for {address}: result window too large "
                    f"at page {page}; tx count may be a lower bound.",
                    file=sys.stderr,
                )
                break
            raise

        result = data.get("result", [])
        if isinstance(result, str) or not result:
            break

        reached_old = False
        for tx in result:
            try:
                ts = int(tx["timeStamp"])
            except (KeyError, ValueError):
                continue

            if ts < cutoff_ts:
                reached_old = True
                break  # sorted desc — all subsequent are older

            count += 1

        if reached_old or len(result) < offset:
            break

        page += 1

    return count


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
) -> Tuple[List[str], Dict[str, str], str]:
    """
    Scrape Etherscan Smart Contract Search results and return discovered addresses.

    Returns:
      - unique addresses in discovery order
      - mapping of address (lowercased) -> discovery page URL
      - param label for logging
    """
    all_addresses: List[str] = []
    address_to_url: Dict[str, str] = {}
    seen: set = set()
    param_label = "q+a=all"

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
                    return all_addresses, address_to_url, param_label

        print(f"  Page {page_num}: {len(page_addrs)} addresses found (total so far: {len(all_addresses)})", flush=True)

        if len(page_addrs) < 100:
            break

    return all_addresses, address_to_url, param_label


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

    cutoff_dt = datetime.now(timezone.utc) - relativedelta(months=args.months)
    cutoff_ts = int(cutoff_dt.timestamp())

    session = requests.Session()
    session.headers.update({"User-Agent": "etherscan-contract-search-scanner/2.0"})

    # ---- Discovery ----------------------------------------------------------
    discovered, address_to_url, param_used = discover_addresses_via_smart_contract_search(
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
    print(f"# Discovery param used        : {param_used}")
    print(f"# Candidate addresses         : {len(discovered)}")

    # ---- Pre-fetch start block for tx window --------------------------------
    start_block: Optional[int] = None
    if args.months > 0:
        start_block = get_block_by_timestamp(
            session, cutoff_ts, args.apikey, args.chainid, args.timeout
        )
        if start_block:
            print(f"# Tx window start block       : {start_block:,}")
        else:
            print(
                "# [warn] getblocknobytime failed; falling back to full history scan",
                file=sys.stderr,
            )

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

        # Tx count
        try:
            recent_tx_count = fetch_recent_normal_txs_count(
                session=session,
                address=address,
                apikey=args.apikey,
                chainid=args.chainid,
                cutoff_ts=cutoff_ts,
                timeout=args.timeout,
                start_block=start_block,
            )
        except EtherscanError as e:
            err_str = str(e).lower()
            if "no transactions found" in err_str or "result=[]" in err_str:
                recent_tx_count = 0
            else:
                print(f"[warn] tx count fetch failed for {address}: {e}", file=sys.stderr)
                continue
        except Exception as e:
            print(f"[warn] tx count fetch failed for {address}: {e}", file=sys.stderr)
            continue

        if recent_tx_count < args.min_txs:
            continue

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
