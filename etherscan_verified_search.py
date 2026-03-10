#!/usr/bin/env python3
"""
Search verified contracts via Etherscan Smart Contract Search, then filter by source usage and recent activity.

What it does:
- Uses Etherscan Smart Contract Search pages as discovery
- Collects candidate verified contract addresses from search results
- Fetches verified source via Etherscan V2 getsourcecode
- Confirms one or more target strings are present in source
- Filters out contracts with fewer than X normal txs in the last Y months
- Prints matching contracts or writes CSV

Requirements:
    pip install requests beautifulsoup4 python-dateutil

Examples:
    python etherscan_smart_search_filter.py \
      --apikey YOUR_KEY \
      --query "0x32400084c286cf3e17e7b677ea9583e60a000324" \
      --strings "0x32400084c286cf3e17e7b677ea9583e60a000324" \
      --min-txs 10 \
      --months 6

    python etherscan_smart_search_filter.py \
      --apikey YOUR_KEY \
      --query "requestL2Transaction" \
      --strings "requestL2Transaction" "0x32400084c286cf3e17e7b677ea9583e60a000324" \
      --min-txs 5 \
      --months 3 \
      --output csv \
      --csv-path matches.csv



Main limitation:

The discovery step is still frontend scraping, because I could not verify an official Etherscan API endpoint for Smart Contract Search results. If Etherscan changes the website parameters or HTML, the script’s discovery logic will need an update.



"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from bs4 import BeautifulSoup
from dateutil.relativedelta import relativedelta


ETHERSCAN_WEB_BASE = "https://etherscan.io"
ETHERSCAN_API_BASE = "https://api.etherscan.io/v2/api"
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")


class EtherscanError(RuntimeError):
    pass


@dataclass
class MatchRow:
    address: str
    contract_name: str
    compiler_version: str
    tx_count_recent: int
    matched_terms: List[str]
    source_files: int
    discovery_url: str


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Search Etherscan Smart Contract Search results, then filter by source usage and recent tx count."
    )
    p.add_argument("--apikey", required=True, help="Etherscan API key")
    p.add_argument("--query", required=True, help="Keyword/address/function name to search in Smart Contract Search")
    p.add_argument("--strings", nargs="+", required=True, help="Strings to confirm inside verified source")
    p.add_argument("--min-txs", type=int, required=True, help="Minimum normal txs in the lookback window")
    p.add_argument("--months", type=int, required=True, help="Lookback window in months")
    p.add_argument("--chainid", default="1", help="Etherscan V2 chainid, default: 1")
    p.add_argument("--max-pages", type=int, default=10, help="Max Smart Contract Search result pages to crawl")
    p.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    p.add_argument("--page-delay", type=float, default=0.35, help="Delay between search page fetches")
    p.add_argument("--api-delay", type=float, default=0.15, help="Delay between API calls")
    p.add_argument("--case-sensitive", action="store_true", help="Use case-sensitive matching for --strings")
    p.add_argument("--stop-after", type=int, default=0, help="Stop after discovering this many addresses")
    p.add_argument("--output", choices=["text", "csv"], default="text", help="Output format")
    p.add_argument("--csv-path", default="matches.csv", help="CSV output path")
    p.add_argument(
        "--search-param-order",
        nargs="*",
        default=["q", "a", "keyword"],
        help="Query parameter names to try against searchcontractlist",
    )
    return p


def etherscan_get(session: requests.Session, params: Dict[str, str], timeout: int) -> dict:
    resp = session.get(ETHERSCAN_API_BASE, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    status = data.get("status")
    result = data.get("result")
    message = data.get("message", "")

    if status == "1":
        return data

    if isinstance(result, str) and result.lower() == "no transactions found":
        return data

    raise EtherscanError(f"Etherscan error: message={message!r}, result={result!r}")


def get_source_code(
    session: requests.Session,
    address: str,
    apikey: str,
    chainid: str,
    timeout: int,
) -> dict:
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
    result = data.get("result", [])
    if not isinstance(result, list) or not result:
        raise EtherscanError(f"No source result for {address}")
    return result[0]


def normalize_source_blob(source_code_field: str) -> Tuple[str, int]:
    """
    Etherscan SourceCode may be:
    - raw Solidity source
    - a standard-json blob wrapped in extra braces
    - a multi-file blob

    For string matching, normalize to one text blob.
    """
    if not source_code_field:
        return "", 0

    text = source_code_field
    if text.startswith("{{") and text.endswith("}}"):
        text = text[1:-1]

    file_count = text.count('"content"')
    if file_count == 0:
        file_count = 1

    return text, file_count


def find_matching_terms(source_blob: str, terms: Sequence[str], case_sensitive: bool) -> List[str]:
    if case_sensitive:
        return [t for t in terms if t in source_blob]
    haystack = source_blob.lower()
    return [t for t in terms if t.lower() in haystack]


def fetch_recent_normal_txs_count(
    session: requests.Session,
    address: str,
    apikey: str,
    chainid: str,
    cutoff_ts: int,
    timeout: int,
    api_delay: float,
) -> int:
    page = 1
    offset = 1000
    count = 0

    while True:
        data = etherscan_get(
            session,
            {
                "chainid": chainid,
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": "0",
                "endblock": "9999999999",
                "page": str(page),
                "offset": str(offset),
                "sort": "desc",
                "apikey": apikey,
            },
            timeout,
        )
        result = data.get("result", [])
        if isinstance(result, str) or not result:
            break

        reached_old = False
        for tx in result:
            try:
                ts = int(tx["timeStamp"])
            except Exception:
                continue

            if ts < cutoff_ts:
                reached_old = True
                continue

            count += 1

        if reached_old or len(result) < offset:
            break

        page += 1
        time.sleep(api_delay)

    return count


def extract_addresses_from_html(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[str] = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True)

        for blob in (href, text):
            for addr in ADDRESS_RE.findall(blob):
                addr_l = addr.lower()
                if addr_l not in seen:
                    seen.add(addr_l)
                    out.append(addr)

    return out


def looks_like_search_results_page(html: str) -> bool:
    text = html.lower()
    return "smart contract search" in text and (
        "loading" in text or "no matching entries" in text or "/address/" in text
    )


def fetch_search_result_page(
    session: requests.Session,
    query: str,
    page_num: int,
    param_name: str,
    timeout: int,
) -> Tuple[str, str]:
    """
    Best-effort fetch of Etherscan Smart Contract Search result pages.

    Because Etherscan does not document a Smart Contract Search API endpoint,
    this function probes a few likely query param names against searchcontractlist.
    """
    url = f"{ETHERSCAN_WEB_BASE}/searchcontractlist"
    params = {param_name: query}
    if page_num > 1:
        params["p"] = str(page_num)

    resp = session.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": "etherscan-smart-contract-search-scanner/1.0"},
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
    search_param_order: Sequence[str],
) -> Tuple[List[str], List[str], str]:
    """
    Returns:
      - unique addresses
      - discovery URLs fetched
      - parameter name that seemed to work

    This is intentionally defensive because the search page is not officially documented as an API.
    """
    successful_param = ""
    all_addresses: List[str] = []
    seen = set()
    fetched_urls: List[str] = []

    for param_name in search_param_order:
        test_html, test_url = fetch_search_result_page(session, query, 1, param_name, timeout)
        if not looks_like_search_results_page(test_html):
            continue

        successful_param = param_name

        for page_num in range(1, max_pages + 1):
            html, final_url = fetch_search_result_page(session, query, page_num, param_name, timeout)
            fetched_urls.append(final_url)
            page_addrs = extract_addresses_from_html(html)

            # Keep only /address/ style candidates from the page contents.
            # The HTML parser already grabs addresses from href/text; dedupe globally here.
            for addr in page_addrs:
                low = addr.lower()
                if low not in seen:
                    seen.add(low)
                    all_addresses.append(addr)
                    if stop_after and len(all_addresses) >= stop_after:
                        return all_addresses, fetched_urls, successful_param

            time.sleep(page_delay)

        return all_addresses, fetched_urls, successful_param

    return all_addresses, fetched_urls, successful_param


def write_csv(rows: Sequence[MatchRow], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "address",
                "contract_name",
                "compiler_version",
                "tx_count_recent",
                "matched_terms",
                "source_files",
                "discovery_url",
            ]
        )
        for row in rows:
            w.writerow(
                [
                    row.address,
                    row.contract_name,
                    row.compiler_version,
                    row.tx_count_recent,
                    "|".join(row.matched_terms),
                    row.source_files,
                    row.discovery_url,
                ]
            )


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
    session.headers.update({"User-Agent": "etherscan-smart-contract-search-scanner/1.0"})

    discovered, fetched_urls, param_used = discover_addresses_via_smart_contract_search(
        session=session,
        query=args.query,
        max_pages=args.max_pages,
        timeout=args.timeout,
        page_delay=args.page_delay,
        stop_after=args.stop_after,
        search_param_order=args.search_param_order,
    )

    if not param_used:
        raise SystemExit(
            "Could not identify a working Smart Contract Search query parameter.\n"
            "Etherscan may have changed its search frontend. "
            "Try inspecting the browser network calls for /searchcontractlist and update --search-param-order."
        )

    matches: List[MatchRow] = []
    scanned = 0
    with_source = 0
    with_confirmed_term_match = 0

    discovery_url = fetched_urls[0] if fetched_urls else f"{ETHERSCAN_WEB_BASE}/searchcontract"

    for address in discovered:
        scanned += 1

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
            time.sleep(args.api_delay)
            continue

        source_code = source_meta.get("SourceCode", "") or ""
        contract_name = source_meta.get("ContractName", "") or ""
        compiler_version = source_meta.get("CompilerVersion", "") or ""

        if not source_code:
            time.sleep(args.api_delay)
            continue

        with_source += 1
        source_blob, source_files = normalize_source_blob(source_code)
        matched_terms = find_matching_terms(source_blob, args.strings, args.case_sensitive)

        if not matched_terms:
            time.sleep(args.api_delay)
            continue

        with_confirmed_term_match += 1

        try:
            recent_tx_count = fetch_recent_normal_txs_count(
                session=session,
                address=address,
                apikey=args.apikey,
                chainid=args.chainid,
                cutoff_ts=cutoff_ts,
                timeout=args.timeout,
                api_delay=args.api_delay,
            )
        except Exception as e:
            print(f"[warn] tx count fetch failed for {address}: {e}", file=sys.stderr)
            time.sleep(args.api_delay)
            continue

        if recent_tx_count < args.min_txs:
            time.sleep(args.api_delay)
            continue

        matches.append(
            MatchRow(
                address=address,
                contract_name=contract_name,
                compiler_version=compiler_version,
                tx_count_recent=recent_tx_count,
                matched_terms=matched_terms,
                source_files=source_files,
                discovery_url=discovery_url,
            )
        )
        time.sleep(args.api_delay)

    matches.sort(key=lambda r: (-r.tx_count_recent, r.address.lower()))

    print(f"# Smart Contract Search query: {args.query}")
    print(f"# Discovery param used: {param_used}")
    print(f"# Discovery pages fetched: {len(fetched_urls)}")
    print(f"# Candidate addresses discovered: {len(discovered)}")
    print(f"# Contracts scanned: {scanned}")
    print(f"# Contracts with source code: {with_source}")
    print(f"# Contracts confirming target strings: {with_confirmed_term_match}")
    print(f"# Min txs in last {args.months} month(s): {args.min_txs}")
    print(f"# Final matches: {len(matches)}")
    print()

    if args.output == "csv":
        write_csv(matches, args.csv_path)
        print(f"Wrote CSV to {args.csv_path}")
        return 0

    for row in matches:
        print(
            f"{row.address} | {row.contract_name or '-'} | "
            f"{row.tx_count_recent} txs/{args.months}m | "
            f"matched: {', '.join(row.matched_terms)}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
