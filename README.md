# RPC Function Usage Scanner

A lightweight Python script to analyze **who is calling a specific function on a smart contract** over the last *N days*, including **internal contract calls**.

The scanner uses **trace RPC APIs** to detect both direct transactions and internal calls, using JSON-RPC batch requests to minimise round-trips.

---

# Features

* Detects **direct and internal function calls** — including `staticcall` and `delegatecall` variants
* Works without Etherscan or external indexers
* Identifies **unique counterparties**
* Classifies addresses as:
  * `[EOA]` — externally owned account
  * `[Contract]` — smart contract
  * `[7702del]` — EIP-7702 delegated account
* Aggregates **number of transactions per caller**
* Batches `eth_getTransactionByHash` and `eth_getCode` lookups for performance
* Displays a **live progress bar** to stderr during scanning and classification

---

# Requirements

* Python **3.9+**
* Dependencies:
```
pip install requests eth-utils
```
* An **RPC provider with tracing enabled** supporting:
```
trace_filter
eth_getCode
eth_getBlockByNumber
eth_getTransactionByHash
```

Example providers that may support this:
* Chainstack (Growth plan or higher)
* Erigon / Nethermind self-hosted nodes
* Some QuickNode trace-enabled endpoints

The script will **fail early** if `trace_filter` is not supported.

---

# Usage

```
python usage_scanner_rpc_only.py \
  --address <contract_address> \
  --signature "<function_signature>" \
  --days <lookback_days> \
  --rpc-url <trace_rpc_endpoint>
```

Example:

```
python usage_scanner_rpc_only.py \
  --address 0x32400084c286cf3e17e7b677ea9583e60a000324 \
  --signature "requestL2Transaction(address,uint256,bytes,uint256,uint256,bytes[],address)" \
  --days 14 \
  --rpc-url https://your-tracing-rpc.example
```

Optional parameters:

| Flag                     | Description                                        |
| ------------------------ | -------------------------------------------------- |
| `--chunk-size`           | Number of blocks per trace query (default `50000`) |
| `--avg-block-time`       | Used for initial block estimation (default `12`)   |
| `--timeout`              | RPC timeout seconds (default `30`)                 |
| `--verbose-trace-errors` | Print per-chunk trace RPC errors to stderr         |

---

# Output Format

Progress and status messages are written to **stderr**. The final result is written to **stdout**, making it safe to redirect or pipe.

Example output:

```
# Contract: 0x32400084C286CF3E17e7B677ea9583e60a000324
# Function signature: requestL2Transaction(address,uint256,bytes,uint256,uint256,bytes[],address)
# Selector: 0xeb672419
# Lookback days: 14
# Cutoff UTC: 2026-02-25T10:00:00+00:00
# Start block: 21938450
# End block: 21972031
# Trace frames seen: 421
# Selector-matching frames: 23
# trace_filter failed chunks: 0
# Matched txs: 17
# Unique counterparties: 6

[EOA] 0x1234567890abcdef1234567890abcdef12345678 (5 txs)
[Contract] 0x98cdabcdef1234567890abcdef1234567890abcd (4 txs)
[EOA] 0x77aabbccdd1234567890abcdef1234567890aabb (3 txs)
```

Each result line shows:

```
[TYPE] ADDRESS (n txs)
```

Where:
* **TYPE**
  * `[EOA]` — externally owned account
  * `[Contract]` — smart contract
  * `[7702del]` — EIP-7702 delegated account
* **ADDRESS** — full checksummed address
* **n txs** — number of **unique transactions** where that address was the immediate caller

Results are sorted by transaction count descending.

If the same address calls the function multiple times in the same transaction, it counts as **1 transaction**.

---

# Limitations

* Requires an RPC provider exposing **`trace_filter`**.
* Infura, Alchemy, and Cloudflare **do not support tracing**.
* Very large time windows may require reducing `--chunk-size` to avoid RPC timeouts.
* `(n txs)` counts **unique transactions**, not individual call frames.
* Assumes **Ethereum-style tracing APIs** (Erigon / OpenEthereum / Nethermind).
* JSON-RPC batch support is required for the tx and code lookups — all major tracing nodes support this.

---

# How It Works

1. Verify RPC supports `trace_filter`
2. Verify target address contains code
3. Convert lookback days → exact block range via binary search on block timestamps
4. Query traces in chunks using:
   ```
   trace_filter(toAddress=[target_contract])
   ```
5. For each chunk, filter frames where:
   ```
   action.input.startswith(function_selector)
   ```
   All call variants are included (call, staticcall, delegatecall, callcode).
6. Batch-resolve tx senders for all matching frames in the chunk via a single `eth_getTransactionByHash` batch request
7. Attribute each frame to the correct counterparty:
   * If `action.from == tx.from` → **direct call**, record `tx.from`
   * If `action.from != tx.from` → **internal call**, record `action.from` (the immediate caller)
8. After scanning, batch-classify all counterparties via a single `eth_getCode` batch request
9. Print results sorted by transaction count

---

# License

MIT
