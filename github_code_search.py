#!/usr/bin/env python3
"""
GitHub Code Search Script

Usage:
    python github_code_search.py \
        --query "(finalizeEthWithdrawal OR requestL2Transaction) language:Solidity" \
        --min-stars 50 \
        --min-activity 90d

Arguments:
    --query         GitHub code search query string.
                    Supports OR expressions with parentheses, e.g.:
                      "(termA OR termB) language:Solidity"
                    These are automatically expanded into separate API calls
                    because the GitHub code search REST API does not support
                    parenthesised OR syntax.
    --min-stars     Minimum number of stars a repo must have
    --min-activity  Minimum activity window (e.g. 7d, 4w, 6m, 1y)
                    Repos whose latest commit is OLDER than this threshold are excluded.
    --token         GitHub personal access token (or set GITHUB_TOKEN env var)
    --per-page      Results per page (max 100, default 100)
    --max-results   Maximum total results to fetch per sub-query (default 1000)
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_duration(value: str) -> timedelta:
    """Parse a human-friendly duration string into a timedelta.

    Supported suffixes: d/day/days, w/week/weeks, m/month/months, y/year/years
    Examples: "7d", "2w", "3m", "1y", "90d", "0d"
    """
    pattern = re.compile(r"^(\d+)\s*([a-zA-Z]*)$")
    match = pattern.match(value.strip())
    if not match:
        raise argparse.ArgumentTypeError(
            f"Cannot parse duration '{value}'. Use formats like 7d, 4w, 6m, 1y, or 0d."
        )
    amount = int(match.group(1))
    unit = match.group(2).lower() or "d"

    if unit in ("d", "day", "days"):
        return timedelta(days=amount)
    elif unit in ("w", "week", "weeks"):
        return timedelta(weeks=amount)
    elif unit in ("m", "month", "months"):
        return timedelta(days=amount * 30)
    elif unit in ("y", "year", "years"):
        return timedelta(days=amount * 365)
    else:
        raise argparse.ArgumentTypeError(
            f"Unknown time unit '{unit}'. Use d, w, m, or y."
        )


def expand_query(query: str) -> list:
    """Expand a query with a parenthesised OR group into multiple simple queries.

    The GitHub code search REST API rejects parentheses and boolean OR.
    We detect patterns like:

        (termA OR termB OR termC) rest-of-qualifiers

    and return one query string per term:

        ["termA rest-of-qualifiers", "termB rest-of-qualifiers", ...]

    If no parenthesised OR group is found, the original query is returned as-is.
    """
    pattern = re.compile(r"^\(([^)]+)\)(.*)", re.DOTALL)
    match = pattern.match(query.strip())
    if not match:
        return [query]

    or_group = match.group(1)
    suffix = match.group(2).strip()

    terms = [t.strip() for t in re.split(r"\bOR\b", or_group, flags=re.IGNORECASE)]
    terms = [t for t in terms if t]

    if len(terms) <= 1:
        return [f"{or_group.strip()} {suffix}".strip()]

    expanded = [f"{term} {suffix}".strip() for term in terms]
    print(f"Expanded OR query into {len(expanded)} sub-queries:")
    for q in expanded:
        print(f"  • {q}")
    return expanded


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

def github_get(url: str, headers: dict, params: dict = None, retry: int = 3):
    """GET request against GitHub API with rate-limit handling."""
    for attempt in range(retry):
        response = requests.get(url, headers=headers, params=params, timeout=30)

        if response.status_code == 200:
            return response

        if response.status_code == 403:
            remaining = response.headers.get("X-RateLimit-Remaining", "?")
            reset_ts = response.headers.get("X-RateLimit-Reset")
            if remaining == "0" and reset_ts:
                wait = max(0, int(reset_ts) - int(time.time())) + 2
                print(f"  [rate limit] sleeping {wait}s …", flush=True)
                time.sleep(wait)
                continue
            wait = 10 * (2 ** attempt)
            print(f"  [403] secondary rate limit? Waiting {wait}s …", flush=True)
            time.sleep(wait)
            continue

        # Return 422 to caller so it can inspect the error message
        if response.status_code == 422:
            return response

        response.raise_for_status()

    return None


def search_code(query: str, headers: dict, per_page: int = 100, max_results: int = 1000):
    """Run a single code search query, returning (total_count, {full_name: html_url})."""
    url = "https://api.github.com/search/code"
    repos_seen: dict = {}
    page = 1
    total_count: Optional[int] = None

    print(f'\nSearching: "{query}"', flush=True)

    while len(repos_seen) < max_results:
        params = {"q": query, "per_page": per_page, "page": page}
        resp = github_get(url, headers, params)

        if resp is None:
            print("  ERROR: no response from GitHub API.", file=sys.stderr)
            break

        if resp.status_code == 422:
            msg = resp.json().get("message", "unknown error")
            print(f"  ERROR 422: {msg}", file=sys.stderr)
            print("  Tip: the REST API does not support parentheses or boolean OR.", file=sys.stderr)
            break

        data = resp.json()

        if total_count is None:
            total_count = data.get("total_count", 0)
            print(f"  → {total_count:,} code matches reported by GitHub", flush=True)

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            repo = item.get("repository", {})
            full_name = repo.get("full_name")
            html_url = repo.get("html_url")
            if full_name and full_name not in repos_seen:
                repos_seen[full_name] = html_url

        if page >= 10 or len(items) < per_page:
            break

        page += 1
        time.sleep(1.0)  # polite delay for expensive search endpoint

    return total_count or 0, repos_seen


def get_repo_details(full_name: str, headers: dict) -> Optional[dict]:
    """Fetch stars and latest push date for a single repo."""
    repo_url = f"https://api.github.com/repos/{full_name}"
    resp = github_get(repo_url, headers)
    if resp is None or resp.status_code != 200:
        return None
    data = resp.json()

    pushed_at = data.get("pushed_at")
    latest_commit_dt = None
    if pushed_at:
        latest_commit_dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))

    return {
        "full_name": full_name,
        "stars": data.get("stargazers_count", 0),
        "latest_commit": latest_commit_dt,
        "html_url": data.get("html_url"),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Search GitHub code and filter repos by stars and recent activity.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--query", required=True, help="GitHub code search query")
    parser.add_argument("--min-stars", type=int, default=0,
                        help="Minimum number of stars (default: 0)")
    parser.add_argument("--min-activity", type=parse_duration, default="90d",
                        metavar="DURATION",
                        help="Keep repos with latest push within this window (e.g. 7d, 4w, 6m, 1y). Default: 90d")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"),
                        help="GitHub personal access token (or set GITHUB_TOKEN env var)")
    parser.add_argument("--per-page", type=int, default=100,
                        help="Results per page, max 100 (default: 100)")
    parser.add_argument("--max-results", type=int, default=1000,
                        help="Max results per sub-query (default: 1000)")
    args = parser.parse_args()

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    else:
        print(
            "WARNING: No GitHub token provided. "
            "The code search endpoint requires authentication.\n"
            "Set --token or GITHUB_TOKEN.",
            file=sys.stderr,
        )

    activity_cutoff = datetime.now(tz=timezone.utc) - args.min_activity
    print(f"Activity cutoff (latest push must be after): {activity_cutoff.date()}")
    print(f"Minimum stars: {args.min_stars}\n")

    # ---- Step 1: expand query and run all sub-searches ----------------------
    sub_queries = expand_query(args.query)

    all_repos: dict = {}       # full_name -> html_url (merged, de-duped)
    total_reported = 0

    for sub_q in sub_queries:
        count, repos = search_code(sub_q, headers,
                                   per_page=min(args.per_page, 100),
                                   max_results=args.max_results)
        total_reported += count
        for full_name, html_url in repos.items():
            all_repos.setdefault(full_name, html_url)

    unique_repos = list(all_repos.keys())
    print(f"\nUnique repos across all sub-queries: {len(unique_repos)}")

    # ---- Step 2: fetch repo details -----------------------------------------
    print("\nFetching repo details …", flush=True)

    details_list = []
    for i, full_name in enumerate(unique_repos, 1):
        print(f"  [{i}/{len(unique_repos)}] {full_name}", flush=True)
        info = get_repo_details(full_name, headers)
        if info is None:
            print(f"    SKIP (could not fetch details)", flush=True)
            continue
        details_list.append(info)
        time.sleep(0.3)

    # ---- Step 3: filter & statistics ----------------------------------------
    below_stars = []
    above_stars_inactive = []
    passing = []

    for info in details_list:
        if info["stars"] < args.min_stars:
            below_stars.append(info)
        elif info["latest_commit"] is None or info["latest_commit"] < activity_cutoff:
            above_stars_inactive.append(info)
        else:
            passing.append(info)

    # ---- Step 4: output ------------------------------------------------------
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    print(f"Total code matches (sum across sub-queries): {total_reported:>6,}")
    print(f"Unique repos fetched:                        {len(details_list):>6,}")
    print(f"Below minimum stars ({args.min_stars}):                  {len(below_stars):>6,}")
    print(f"Above min stars but inactive                 {len(above_stars_inactive):>6,}")
    print(f"  (latest push before {activity_cutoff.date()})")
    print(f"Repos PASSING all filters:                   {len(passing):>6,}")
    print("=" * 60)

    if passing:
        print("\nFILTERED REPO URLs:")
        for info in sorted(passing, key=lambda x: x["stars"], reverse=True):
            commit_str = info["latest_commit"].date() if info["latest_commit"] else "unknown"
            print(f"  {info['html_url']}  (stars={info['stars']}, last_push={commit_str})")
    else:
        print("\nNo repos matched all filters.")


if __name__ == "__main__":
    main()
