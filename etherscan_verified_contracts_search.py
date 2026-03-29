#!/usr/bin/env python3
"""
Search verified contracts via Etherscan Smart Contract Search, filter by recent activity,
and report line-level matches of the search query in verified source code.

What it does:
- Scrapes Etherscan Smart Contract Search pages as discovery
- Collects candidate verified contract addresses
- Fetches verified source via Etherscan V2 getsourcecode
- Locates every occurrence of the search query in source, with filename and line number
- Filters out contracts with fewer than X transactions in the last Y months
- Counts direct txs via Etherscan txlist API
- Counts internal delegatecall txs by scraping the Etherscan internal txs HTML page (via curl)
- Prints matching contracts or writes CSV

Requirements:
    pip install requests beautifulsoup4 python-dateutil tqdm
    curl must be available in PATH

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
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import requests
from dateutil.relativedelta import relativedelta
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


ETHERSCAN_WEB_BASE  = "https://etherscan.io"
ETHERSCAN_API_BASE  = "https://api.etherscan.io/v2/api"
INTERNAL_TX_URL     = (
    "https://etherscan.io/txsInternal"
    "?ps=25&zero=false&a={address}&valid=all&m=advanced"
)
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")


# ---------------------------------------------------------------------------
# Token-bucket rate limiter (Etherscan API calls)
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, calls_per_second: float = 2.5):
        self._interval = 1.0 / calls_per_second
        self._lock     = threading.Lock()
        self._last     = 0.0

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
    filename: str
    line: int

    def __str__(self) -> str:
        return f"{self.filename}#{self.line}"


@dataclass
class MatchRow:
    address:          str
    contract_name:    str
    compiler_version: str
    direct_tx_count:  int
    internal_tx_count: int
    match_locations:  List[MatchLocation]
    discovery_url:    str

    @property
    def total_tx_count(self) -> int:
        return self.direct_tx_count + self.internal_tx_count


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
    p.add_argument("--apikey",    required=True, help="Etherscan API key")
    p.add_argument("--query",     required=True,
                   help="Keyword/function name to search in Smart Contract Search and locate in source")
    p.add_argument("--min-txs",   type=int, required=True,
                   help="Minimum total transactions (direct + internal delegatecall) in the lookback window")
    p.add_argument("--months",    type=int, required=True,
                   help="Lookback window in months")
    p.add_argument("--chainid",   default="1",
                   help="Etherscan V2 chainid (default: 1)")
    p.add_argument("--max-pages", type=int, default=10,
                   help="Max Smart Contract Search result pages to crawl")
    p.add_argument("--timeout",   type=int, default=30,
                   help="HTTP timeout in seconds")
    p.add_argument("--page-delay", type=float, default=0.5,
                   help="Delay between search page fetches (seconds)")
    p.add_argument("--case-sensitive", action="store_true",
                   help="Case-sensitive query matching in source code")
    p.add_argument("--stop-after", type=int, default=0,
                   help="Stop after discovering this many addresses (0 = unlimited)")
    p.add_argument("--output",    choices=["text", "csv"], default="text",
                   help="Output format")
    p.add_argument("--csv-path",  default="matches.csv",
                   help="CSV output path")
    return p


# ---------------------------------------------------------------------------
# Etherscan API helpers
# ---------------------------------------------------------------------------

def etherscan_get(
    session:     requests.Session,
    params:      Dict[str, str],
    timeout:     int,
    max_retries: int = 4,
) -> dict:
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
            print(
                f"  [HTTP {resp.status_code}] retrying in {wait}s "
                f"(attempt {attempt + 1}/{max_retries})",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        resp.raise_for_status()
        data   = resp.json()
        status = data.get("status")
        result = data.get("result")
        message = data.get("message", "")

        if status == "1":
            return data

        if isinstance(result, str) and "no " in result.lower():
            return data

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
        f"Etherscan API call failed after {max_retries} attempts. Params: {params}"
    )


def get_source_code(
    session:  requests.Session,
    address:  str,
    apikey:   str,
    chainid:  str,
    timeout:  int,
) -> dict:
    data = etherscan_get(
        session,
        {
            "chainid": chainid,
            "module":  "contract",
            "action":  "getsourcecode",
            "address": address,
            "apikey":  apikey,
        },
        timeout,
    )
    results = data.get("result", [])
    if not results or not isinstance(results, list):
        raise EtherscanError(f"No source result for {address}")
    return results[0]


# ---------------------------------------------------------------------------
# Direct tx count — Etherscan txlist API
# ---------------------------------------------------------------------------

def get_direct_tx_count(
    session:    requests.Session,
    address:    str,
    apikey:     str,
    chainid:    str,
    timeout:    int,
    start_time: int,          # unix timestamp of window start
) -> int:
    """
    Count direct (non-internal) transactions to/from `address` since
    `start_time` using the Etherscan txlist API.

    Paginates automatically (up to 10,000 results per page at page_size=10000).
    Returns the number of txs whose timestamp >= start_time.
    """
    page      = 1
    page_size = 10_000
    count     = 0

    while True:
        try:
            data = etherscan_get(
                session,
                {
                    "chainid":   chainid,
                    "module":    "account",
                    "action":    "txlist",
                    "address":   address,
                    "startblock": "0",
                    "endblock":  "99999999",
                    "page":      str(page),
                    "offset":    str(page_size),
                    "sort":      "desc",
                    "apikey":    apikey,
                },
                timeout,
            )
        except EtherscanError:
            break

        txs = data.get("result") or []
        if not isinstance(txs, list) or not txs:
            break

        for tx in txs:
            ts = int(tx.get("timeStamp", 0))
            if ts >= start_time:
                count += 1
            else:
                # Results are sorted desc — once we pass the window, stop
                return count

        if len(txs) < page_size:
            break

        page += 1

    return count


# ---------------------------------------------------------------------------
# Internal delegatecall count — HTML scraping via curl
# ---------------------------------------------------------------------------

def _fetch_internal_tx_html(address: str, timeout: int) -> str:
    """Fetch Etherscan internal txs page for `address` using curl."""
    url = INTERNAL_TX_URL.format(address=address.lower())

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [
                "curl", "-s", "-L",
                "--compressed",
                "--max-time", str(timeout),
                "-o", tmp_path,
                "-w", "%{http_code}",
                url,
            ],
            capture_output=True,
            text=True,
        )

        http_code = result.stdout.strip()

        if result.returncode != 0:
            raise RuntimeError(f"curl failed (exit {result.returncode}): {result.stderr.strip()}")
        if http_code != "200":
            raise RuntimeError(f"HTTP {http_code} fetching internal txs for {address}")

        with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _extract_internal_tx_rows(html: str) -> list:
    """
    Extract the quickExportTxsInternalData JSON embedded in Etherscan's HTML.
    Propagates DateTime forward for sub-rows that share a parent transaction.
    """
    pattern = r"quickExportTxsInternalData\s*=\s*'(\[.*?\])'"
    match   = re.search(pattern, html, re.DOTALL)
    if not match:
        return []   # no internal txs, or page structure changed

    rows    = json.loads(match.group(1))
    last_dt = ""
    for row in rows:
        if row.get("DateTime"):
            last_dt = row["DateTime"]
        else:
            row["DateTime"] = last_dt
    return rows


def get_internal_delegatecall_count(
    address:    str,
    timeout:    int,
    start_time: int,   # unix timestamp of window start
) -> int:
    """
    Count internal delegatecall transactions to `address` since `start_time`
    by scraping the Etherscan internal transactions HTML page via curl.

    Note: Etherscan's internal txs page is paginated at 25 rows per page.
    This function only fetches the first page (the 25 most recent rows).
    For addresses with very high internal tx volume you may undercount, but
    for discovery / filtering purposes this is a practical first-pass filter.
    """
    try:
        html = _fetch_internal_tx_html(address, timeout)
    except RuntimeError as e:
        print(f"  [warn] internal tx fetch failed for {address}: {e}", file=sys.stderr)
        return 0

    rows  = _extract_internal_tx_rows(html)
    count = 0

    for row in rows:
        if row.get("Type", "").lower() != "delegatecall":
            continue
        dt_str = row.get("DateTime", "")
        if not dt_str:
            continue
        try:
            tx_time = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if int(tx_time.timestamp()) >= start_time:
            count += 1

    return count


# ---------------------------------------------------------------------------
# Source code parsing
# ---------------------------------------------------------------------------

def find_query_in_source(
    source_code_field: str,
    query:             str,
    case_sensitive:    bool,
) -> List[MatchLocation]:
    if not source_code_field:
        return []

    needle    = query if case_sensitive else query.lower()
    locations: List[MatchLocation] = []

    def scan_text(text: str, filename: str) -> None:
        for lineno, line in enumerate(text.splitlines(), start=1):
            haystack = line if case_sensitive else line.lower()
            if needle in haystack:
                locations.append(MatchLocation(filename=filename, line=lineno))

    text = source_code_field.strip()

    if text.startswith("{{") and text.endswith("}}"):
        try:
            obj     = json.loads(text[1:-1])
            sources = obj.get("sources", {})
            if sources:
                for fname, fdata in sources.items():
                    content = fdata.get("content", "")
                    if content:
                        scan_text(content, fname)
                return locations
        except (json.JSONDecodeError, AttributeError):
            pass

    if text.startswith("{"):
        try:
            obj     = json.loads(text)
            content = obj.get("SourceCode") or obj.get("content") or ""
            if content:
                scan_text(content, "contract.sol")
                return locations
        except (json.JSONDecodeError, AttributeError):
            pass

    scan_text(text, "contract.sol")
    return locations


# ---------------------------------------------------------------------------
# Discovery (web scraping)
# ---------------------------------------------------------------------------

def extract_addresses_from_html(html: str) -> List[str]:
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
    session:   requests.Session,
    query:     str,
    page_num:  int,
    timeout:   int,
    page_size: int = 100,
) -> Tuple[str, str]:
    url    = f"{ETHERSCAN_WEB_BASE}/searchcontractlist"
    params = {"a": "all", "q": query, "ps": str(page_size), "p": str(page_num)}
    resp   = session.get(
        url, params=params, timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 (compatible; etherscan-contract-search-scanner/2.0)"},
    )
    resp.raise_for_status()
    return resp.text, resp.url


def discover_addresses(
    session:    requests.Session,
    query:      str,
    max_pages:  int,
    timeout:    int,
    page_delay: float,
    stop_after: int,
) -> Tuple[List[str], Dict[str, str]]:
    all_addresses:  List[str]       = []
    address_to_url: Dict[str, str]  = {}
    seen:           set             = set()

    for page_num in range(1, max_pages + 1):
        if page_num > 1:
            time.sleep(page_delay)

        html, final_url = fetch_search_result_page(session, query, page_num, timeout)
        page_addrs      = extract_addresses_from_html(html)

        if not page_addrs:
            if page_num == 1:
                print(
                    f"  [warn] page 1 returned no addresses. Check: {final_url}",
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

        print(
            f"  Page {page_num}: {len(page_addrs)} addresses "
            f"(total: {len(all_addresses)})",
            flush=True,
        )

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
                "direct_txs", "internal_delegatecall_txs", "total_txs",
                f"matched:{query}", "discovery_url",
            ])
            for row in rows:
                locs = ", ".join(str(loc) for loc in row.match_locations)
                w.writerow([
                    row.address,
                    row.contract_name,
                    row.compiler_version,
                    row.direct_tx_count,
                    row.internal_tx_count,
                    row.total_tx_count,
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

    cutoff_dt  = datetime.now(timezone.utc) - relativedelta(months=args.months)
    start_time = int(cutoff_dt.timestamp())

    session = requests.Session()
    session.headers.update({"User-Agent": "etherscan-contract-search-scanner/2.0"})

    # ---- Discovery ----------------------------------------------------------
    print(f"Discovering contracts matching: {args.query!r}")
    discovered, address_to_url = discover_addresses(
        session    = session,
        query      = args.query,
        max_pages  = args.max_pages,
        timeout    = args.timeout,
        page_delay = args.page_delay,
        stop_after = args.stop_after,
    )

    if not discovered:
        raise SystemExit(
            "No addresses discovered. Either the query returned no results "
            "or Etherscan has changed its HTML structure.\n"
            f"Verify manually: {ETHERSCAN_WEB_BASE}/searchcontractlist?a=all&q={args.query}&ps=100&p=1"
        )

    print(f"\n# Smart Contract Search query : {args.query}")
    print(f"# Candidate addresses         : {len(discovered)}")
    print(f"# Lookback window             : {args.months} month(s) (since {cutoff_dt.date()})")
    print()

    # ---- Per-contract filtering ---------------------------------------------
    matches:          List[MatchRow] = []
    scanned          = 0
    with_source      = 0
    with_query_match = 0
    fallback_url     = f"{ETHERSCAN_WEB_BASE}/searchcontract"

    _iter = tqdm(discovered, desc="Scanning", unit="contract") if tqdm else discovered

    for address in _iter:
        scanned += 1
        addr_l        = address.lower()
        discovery_url = address_to_url.get(addr_l, fallback_url)

        # ---- Source code ----------------------------------------------------
        try:
            source_meta = get_source_code(
                session = session,
                address = address,
                apikey  = args.apikey,
                chainid = args.chainid,
                timeout = args.timeout,
            )
        except Exception as e:
            print(f"[warn] source fetch failed for {address}: {e}", file=sys.stderr)
            continue

        source_code      = source_meta.get("SourceCode", "") or ""
        contract_name    = source_meta.get("ContractName", "") or ""
        compiler_version = source_meta.get("CompilerVersion", "") or ""

        if not source_code:
            continue

        with_source += 1

        # ---- Query match in source ------------------------------------------
        locations = find_query_in_source(source_code, args.query, args.case_sensitive)
        if not locations:
            continue

        with_query_match += 1

        # ---- Tx counting ----------------------------------------------------
        # 1. Direct txs via Etherscan txlist API
        direct_count = get_direct_tx_count(
            session    = session,
            address    = address,
            apikey     = args.apikey,
            chainid    = args.chainid,
            timeout    = args.timeout,
            start_time = start_time,
        )

        # 2. Internal delegatecall txs via HTML scraping (curl)
        internal_count = get_internal_delegatecall_count(
            address    = address,
            timeout    = args.timeout,
            start_time = start_time,
        )

        total_count = direct_count + internal_count

        if total_count < args.min_txs:
            continue

        matches.append(MatchRow(
            address           = address,
            contract_name     = contract_name,
            compiler_version  = compiler_version,
            direct_tx_count   = direct_count,
            internal_tx_count = internal_count,
            match_locations   = locations,
            discovery_url     = discovery_url,
        ))

    matches.sort(key=lambda r: (-r.total_tx_count, r.address.lower()))

    # ---- Summary ------------------------------------------------------------
    print(f"# Contracts scanned           : {scanned}")
    print(f"# Contracts with source code  : {with_source}")
    print(f"# Source confirms query match : {with_query_match}")
    print(f"# Min txs threshold           : {args.min_txs}")
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
            f"direct={row.direct_tx_count} internal={row.internal_tx_count} "
            f"total={row.total_tx_count}/{args.months}m | "
            f"matched: {args.query} [{locs}]"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())