#!/usr/bin/env python3
"""
Probe a tracing RPC for its maximum trace_filter chunk size.

Usage:
    python probe_chunk_size.py \\
        --rpc-url https://your-tracing-rpc.example \\
        --address 0xE592427A0AEce92De3Edee1F18E0157C05861564   # Uniswap V3 SwapRouter (busy)

Why this address? A high-traffic contract surfaces payload-size limits
faster than a quiet one: the same 10,000-block range produces a much
bigger response when the target is busy, which is what we actually want
to measure.

The script tries chunk sizes from small to large, stopping at the first
failure, then binary-searches between the last success and the first
failure to pinpoint the ceiling.
"""

from __future__ import annotations

import argparse
import sys
import time

import requests


def trace_filter(rpc_url: str, from_block: int, to_block: int, address: str,
                 timeout: int = 60) -> tuple[bool, str, float, int]:
    """
    Returns (ok, message, elapsed_seconds, response_size_bytes).
    `ok=False` means the call failed for any reason.
    """
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "trace_filter",
        "params": [{
            "fromBlock": hex(from_block),
            "toBlock":   hex(to_block),
            "toAddress": [address],
        }],
    }
    t0 = time.time()
    try:
        r = requests.post(rpc_url, json=payload, timeout=timeout)
    except requests.Timeout:
        return False, "TIMEOUT", time.time() - t0, 0
    except requests.RequestException as e:
        return False, f"NETWORK: {e}", time.time() - t0, 0

    elapsed = time.time() - t0
    size = len(r.content)

    if r.status_code != 200:
        snippet = r.text[:200].replace("\n", " ")
        return False, f"HTTP {r.status_code}: {snippet}", elapsed, size

    try:
        data = r.json()
    except ValueError:
        return False, "INVALID JSON", elapsed, size

    if "error" in data:
        return False, f"RPC error: {data['error']}", elapsed, size

    result = data.get("result")
    if not isinstance(result, list):
        return False, f"unexpected result shape: {type(result).__name__}", elapsed, size

    return True, f"{len(result)} frames", elapsed, size


def get_latest_block(rpc_url: str, timeout: int = 15) -> int:
    r = requests.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
        timeout=timeout,
    )
    r.raise_for_status()
    return int(r.json()["result"], 16)


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def probe(rpc_url: str, address: str, end_block: int, timeout: int) -> int | None:
    """
    Phase 1: exponential ramp-up until first failure.
    Phase 2: binary search between last success and first failure.
    Returns the largest known-good chunk size.
    """
    candidates = [100, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000]

    last_ok: int | None = None
    first_fail: int | None = None

    print("=" * 70)
    print("Phase 1: exponential ramp-up")
    print("=" * 70)
    for size in candidates:
        a = end_block - size + 1
        b = end_block
        ok, msg, elapsed, bytes_received = trace_filter(rpc_url, a, b, address, timeout)
        marker = "OK " if ok else "FAIL"
        print(f"  [{marker}] chunk={size:>6d} blocks  "
              f"time={elapsed:5.2f}s  size={fmt_bytes(bytes_received):>8s}  {msg[:80]}")
        if ok:
            last_ok = size
        else:
            first_fail = size
            break

    if first_fail is None:
        print(f"\nAll sampled sizes succeeded. Ceiling is at or above {last_ok}.")
        print("Rerun with higher upper bounds if you want to find the true ceiling.")
        return last_ok

    if last_ok is None:
        print(f"\nEven the smallest probe ({candidates[0]}) failed.")
        print("Check RPC connectivity, tracing tier access, or reduce the start size.")
        return None

    print()
    print("=" * 70)
    print(f"Phase 2: binary search between {last_ok} (ok) and {first_fail} (fail)")
    print("=" * 70)
    lo, hi = last_ok, first_fail
    while hi - lo > max(50, lo // 10):    # stop when we're within ~10%
        mid = (lo + hi) // 2
        a = end_block - mid + 1
        b = end_block
        ok, msg, elapsed, bytes_received = trace_filter(rpc_url, a, b, address, timeout)
        marker = "OK " if ok else "FAIL"
        print(f"  [{marker}] chunk={mid:>6d} blocks  "
              f"time={elapsed:5.2f}s  size={fmt_bytes(bytes_received):>8s}  {msg[:80]}")
        if ok:
            lo = mid
        else:
            hi = mid

    return lo


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rpc-url", required=True)
    p.add_argument("--address", required=True,
                   help="A busy contract (Uniswap router, WETH, USDC, etc.)")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--offset-from-head", type=int, default=100,
                   help="blocks behind head to probe (avoid reorg-prone tip; default 100)")
    args = p.parse_args()

    print(f"Probing {args.rpc_url}\nwith toAddress={args.address}\n")

    latest = get_latest_block(args.rpc_url, args.timeout)
    end_block = latest - args.offset_from_head
    print(f"Latest block: {latest}")
    print(f"Probing against range ending at block {end_block}")
    print()

    best = probe(args.rpc_url, args.address, end_block, args.timeout)

    if best is None:
        print("\nNo safe chunk size found. Is the RPC tracing-enabled?")
        return 1

    print()
    print("=" * 70)
    print(f"Recommended --chunk-size: {int(best * 0.8)}   (80% of measured ceiling)")
    print(f"Measured ceiling:         {best}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
