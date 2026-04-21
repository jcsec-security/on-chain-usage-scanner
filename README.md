# Deprecated Function & Contract Usage Research Toolkit

A set of four scripts to systematically research the real-world usage of a contract, an address, or specific functions that are planned for deprecation. Each step answers a different dimension of the research question.

Steps 1–3 focus on **function-level** research (who invokes a specific function). Step 4 addresses **contract-level** research (which contracts reference a specific address in their bytecode, state, or events).

```
Step 1 — Who calls the function on-chain?         on_chain_target_interactions.py
Step 2 — Who has integrated it in verified code?  etherscan_verified_contracts_search.py
Step 3 — Who references it on GitHub?             github_code_search.py
Step 4 — Which contracts reference an address?    find_address_refs.py
```

---

## Requirements

```bash
pip install requests eth-utils "eth-hash[pycryptodome]" beautifulsoup4 python-dateutil tqdm
```

`curl` must also be available in `PATH` (standard on macOS and Linux).

Additional per-script requirements are noted in each section below.

---

## Step 1 — On-chain Interactions: `on_chain_target_interactions.py`

### Description

Scans the blockchain for all addresses that have called a specific function on a target contract within a given time window. It uses `trace_filter` over a tracing-enabled JSON-RPC endpoint (such as a Chainstack archive node running Erigon) to collect every call frame reaching the target — including direct top-level calls and internal calls from intermediate contracts — and filters them by the function selector derived from the provided canonical signature.

For each unique counterparty found, it reports:
- The address
- Whether it is an EOA, a contract, or an EIP-7702 delegator (`[EOA]`, `[Contract]`, `[7702del]`)
- The number of distinct transactions it sent

The block range is resolved automatically from the `--days` argument using a binary-search refinement on block timestamps.

The low-level RPC primitives and trace helpers live in the shared `ethrpc/` package, also used by Step 4.

### Usage

```bash
python on_chain_target_interactions.py \
  --address 0xYourTargetContract \
  --signature "requestL2Transaction(address,uint256,bytes,uint256,uint256,bytes[],address)" \
  --days 90 \
  --rpc-url https://your-tracing-rpc.example
```

**All arguments:**

| Argument | Required | Description |
|---|---|---|
| `--address` | ✅ | Target contract address |
| `--signature` | — | Canonical function signature (used to derive the 4-byte selector). Omit to match any selector |
| `--days` | ✅ | Lookback window in days |
| `--rpc-url` | ✅ | Tracing-enabled JSON-RPC endpoint |
| `--chunk-size` | — | Blocks per `trace_filter` chunk (default: 1000) |
| `--avg-block-time` | — | Fallback block time in seconds for initial range estimate (default: 12) |
| `--timeout` | — | HTTP timeout in seconds (default: 30) |
| `--verbose-trace-errors` | — | Print per-chunk `trace_filter` errors to stderr |

**Tips:**
- The RPC endpoint **must** support `trace_filter`. Standard Infura/Alchemy public endpoints do not. Use a Chainstack archive node or a self-hosted Erigon/OpenEthereum node.
- Start with a smaller `--days` window to estimate runtime before running the full lookback.
- Reduce `--chunk-size` if the RPC node imposes a block-range limit per call (some nodes cap at 250 or 500 blocks).
- The canonical signature must match exactly, including parameter types — `uint` vs `uint256` will produce a different selector.

### Limitations

- Requires a tracing-capable archive node; not compatible with standard public RPC endpoints.
- Only scans one function selector at a time. Run the script multiple times for multiple functions.
- `trace_filter` can be slow over large block ranges. A 90-day window at chunk size 1000 may take tens of minutes depending on node performance.
- Counterparty classification (`[EOA]`/`[Contract]`) reflects the **current** state of the address, not its state at the time of the transaction.

---

## Step 2 — Verified Contract Integrations: `etherscan_verified_contracts_search.py`

### Description

Discovers verified Solidity contracts on Etherscan that reference a given function name or keyword, confirms the match exists in their source code at the exact file and line level, and filters out contracts with insufficient recent transaction activity. This identifies deployed integrators: contracts that have actually called or wrapped the target function in production.

The script operates in three internal phases:

1. **Discovery** — Scrapes Etherscan's Smart Contract Search (`/searchcontractlist`) to collect candidate contract addresses.
2. **Source confirmation** — Fetches each contract's verified source via the Etherscan V2 `getsourcecode` API and locates every occurrence of the query string, reporting filename and line number. Handles raw Solidity, standard-JSON multi-file layouts, and single-file JSON.
3. **Activity filtering** — Counts recent transactions for each matched contract: direct transactions via the `txlist` API, and internal delegatecall transactions by scraping the Etherscan internal transactions HTML page via `curl`. Only contracts meeting the `--min-txs` threshold pass.

### Usage

```bash
python etherscan_verified_contracts_search.py \
  --apikey YOUR_ETHERSCAN_API_KEY \
  --query "requestL2Transaction" \
  --min-txs 10 \
  --months 6
```

**All arguments:**

| Argument | Required | Description |
|---|---|---|
| `--apikey` | ✅ | Etherscan API key |
| `--query` | ✅ | Keyword or function name to search for |
| `--min-txs` | ✅ | Minimum total transactions (direct + delegatecall) in the lookback window |
| `--months` | ✅ | Lookback window in months |
| `--chainid` | — | Etherscan V2 chain ID (default: `1` for Ethereum mainnet) |
| `--max-pages` | — | Maximum search result pages to crawl (default: 10, each page returns up to 100 addresses) |
| `--stop-after` | — | Stop after discovering N addresses — useful for quick test runs |
| `--page-delay` | — | Delay in seconds between search page fetches (default: 0.5) |
| `--case-sensitive` | — | Enable case-sensitive query matching in source code |
| `--output` | — | Output format: `text` (default) or `csv` |
| `--csv-path` | — | Path for CSV output (default: `matches.csv`) |

**Tips:**
- Use `--stop-after 20` during testing to avoid a full crawl while validating the query.
- Use `--output csv` when you need to pass the results to another tool or process them further.
- A free Etherscan API key is sufficient but rate-limited to ~3 calls/second. The built-in rate limiter handles this automatically.
- The query is matched as a plain substring in source code. To reduce false positives on common terms, use a more specific string such as the full function signature.
- `curl` is used instead of the `requests` library for fetching internal transaction HTML, as Etherscan's bot protection blocks automated `requests` calls but not `curl`.

### Limitations

- Discovery relies on scraping Etherscan's frontend search, which is not an official API. If Etherscan changes its HTML structure, the discovery step will break.
- Etherscan's Smart Contract Search is capped at 10,000 results (100 per page × 100 pages). Queries for very common terms may not surface all relevant contracts.
- Internal transaction counting only fetches the **first page of 25 rows** from the Etherscan internal transactions page. Contracts with more than 25 internal delegatecalls in the window will be undercounted, but will still pass the filter as long as their combined count meets `--min-txs`.
- Only contracts with **verified source code** on Etherscan are considered. Unverified integrations are invisible to this step.
- The query is matched at the text level in source, so it will match comments, strings, and variable names in addition to actual function calls.

---

## Step 3 — GitHub Repository References: `github_code_search.py`

### Description

Searches GitHub for repositories that reference the target function name or keyword in their code, then filters results by minimum star count and recent commit activity. This surfaces off-chain tooling, SDKs, frontends, and protocol integrations that have not yet deployed on-chain or whose on-chain contract is not verified — categories invisible to the first two steps.

The script uses the GitHub Code Search REST API. Because that API does not support boolean `OR` or parenthesised expressions, queries using `(termA OR termB)` syntax are automatically expanded into separate sub-queries and deduplicated before the filtering stage.

### Usage

```bash
python github_code_search.py \
  --query "requestL2Transaction language:Solidity" \
  --min-stars 10 \
  --min-activity 180d \
  --token YOUR_GITHUB_TOKEN
```

**OR query example:**
```bash
python github_code_search.py \
  --query "(finalizeEthWithdrawal OR requestL2Transaction) language:Solidity" \
  --min-stars 10 \
  --min-activity 180d \
  --token YOUR_GITHUB_TOKEN
```

**All arguments:**

| Argument | Required | Description |
|---|---|---|
| `--query` | ✅ | GitHub code search query. Supports `(termA OR termB) qualifiers` syntax |
| `--token` | ✅ (recommended) | GitHub personal access token. Can also be set via `GITHUB_TOKEN` env var. The code search endpoint requires authentication |
| `--min-stars` | — | Minimum repository star count (default: 0) |
| `--min-activity` | — | Only keep repos with a push more recent than this window. Accepts `7d`, `4w`, `6m`, `1y`, etc. (default: `90d`) |
| `--per-page` | — | Results per page, max 100 (default: 100) |
| `--max-results` | — | Maximum results to fetch per sub-query (default: 1000) |

**Tips:**
- Always provide a `--token`. The code search endpoint is unauthenticated-hostile and will rate-limit aggressively without one.
- Add `language:Solidity` to the query to restrict results to Solidity files and reduce noise.
- Use `--min-stars 0 --min-activity 0d` to see all results with no filtering applied, then tune thresholds from there.
- The `--min-activity` window is based on the repository's latest push date (`pushed_at`), not individual file commit dates. A repo with a recent unrelated commit but stale target code will still pass.
- For large result sets, GitHub's code search is capped at 1,000 results per query (10 pages × 100 per page). The `--max-results` argument cannot exceed this GitHub-imposed ceiling.

### Limitations

- GitHub Code Search is capped at **1,000 results per query**. For common function names, a large fraction of matching repositories will not appear in results.
- The API reports a `total_count` that often far exceeds the 1,000 results actually retrievable. This number is indicative only.
- `pushed_at` (the latest push date used for activity filtering) reflects any push to the repository, not necessarily to the file containing the search match. A repo can pass the activity filter while the relevant code is stale.
- Private repositories are not searchable regardless of token permissions. Forks may appear in results and inflate counts.
- The code search endpoint has a secondary rate limit that is undocumented. The script handles `403` responses with exponential back-off but very large crawls may still be throttled significantly.

---

## Step 4 — Contracts Referencing a Target Address: `find_address_refs.py`

### Description

Finds deployed contracts on Ethereum mainnet that reference a specific target address anywhere — either hardcoded in their bytecode (`address constant`, `immutable`, constructor arguments), passed into them via setter functions (externally or internally), or emitted in events. Designed for the case where the deprecation target is an entire contract address rather than a specific function.

The script runs a three-stage pipeline:

1. **Discovery** — Four parallel SQL queries on Dune Analytics (templates in `dune_queries.sql`) scan Ethereum's creation traces, transactions, internal traces, and event logs for any encoding of the target address. Each match is tagged with its source.

2. **Activity filter** (opt-in) — Counts unique transactions (direct + internal) hitting each candidate over the last N days via `trace_filter` on a tracing-enabled RPC, and drops candidates below `--min-txs`. Uses the same `ethrpc` primitives as Step 1.

3. **Storage verification** (default on) — Reads each candidate's first 50 storage slots via `eth_getStorageAt` and drops contracts that don't currently hold the address at any checked slot. Contracts whose *only* signal was the bytecode scan are exempt — `address constant` and `immutable` declarations live in runtime bytecode rather than storage, so the check would reliably misfire on them.

Output is one CSV per selected source plus a merged `results_merged.csv`, with optional Etherscan URL columns for easy spot-checking.

### Setup

1. Save each of the four queries in `dune_queries.sql` as a separate saved query on Dune Analytics. Each accepts two text parameters: `target_address_raw` and `target_address_padded`.
2. Copy each saved query's ID from its URL and edit the `DEFAULT_QUERY_IDS` dict at the top of `find_address_refs.py` (or pass them via `--query-*` CLI flags per-invocation).

### Usage

```bash
python find_address_refs.py 0x57891966931Eb4Bb6FB81430E6cE0A03AAbDe063 \
  --dune-api-key YOUR_DUNE_KEY \
  --rpc-url https://your-tracing-rpc.example \
  --etherscan-links \
  --min-txs 10 --window-days 30 \
  --out-dir ./results
```

**All arguments:**

| Argument | Required | Description |
|---|---|---|
| `address` (positional) | ✅ | Target address to search for (0x-prefixed, 20 bytes) |
| `--dune-api-key` | ✅ | Dune Analytics API key (or set `DUNE_API_KEY` env var) |
| `--rpc-url` | ✅ when `--verify` or `--min-txs` > 0 | Ethereum RPC URL (or set `ETH_RPC_URL` env var). Needs a TRACING RPC for `--min-txs` |
| `--sources` | — | Subset of `bytecode,tx_input,trace_input,event_log`, or `all` (default) |
| `--query-bytecode` / `--query-tx` / `--query-trace` / `--query-log` | — | Dune saved-query IDs; override the `DEFAULT_QUERY_IDS` dict |
| `--out-dir` | — | Directory for output CSVs (default: current dir) |
| `--etherscan-links` | — | Add clickable Etherscan URL columns to CSV output |
| `--min-txs` | — | Activity filter threshold in the window (default: 0 = filter disabled) |
| `--window-days` | — | Activity window in days (default: 30) |
| `--chunk-size` | — | Blocks per `trace_filter` chunk (default: 1000) |
| `--trace-workers` | — | Parallel `trace_filter` chunks (default: 1; raise on tolerant providers) |
| `--avg-block-time` | — | Seconds per block for initial range estimate (default: 12) |
| `--trace-timeout` | — | Per-RPC-call timeout in seconds (default: 60) |
| `--verify` / `--no-verify` | — | Storage verification step (default: on) |
| `--verify-slots` | — | Slots per contract scanned by verify (default: 50) |
| `--verify-top` | — | Verify only the top N candidates (0 = all passing filter) |

### Output

One CSV per selected source (`results_bytecode.csv`, `results_tx_input.csv`, `results_trace_input.csv`, `results_event_log.csv`), plus `results_merged.csv` when more than one source ran. Each row contains:

- `source` — which query surfaced the hit
- `contract_address` — the contract holding/referencing the target
- `tx_hash` — a sample transaction (deploy tx for bytecode hits, setter tx for others)
- `etherscan_contract`, `etherscan_tx` — optional URL columns (with `--etherscan-links`)
- `storage_slots_matched` — only when `--verify` is on:
  - `exempt`: bytecode-only candidate, not checked
  - `3,7`: slot indices where the address was found
  - `unchecked`: outside `--verify-top` window

The merged CSV additionally has `sources` (pipe-separated list) and `source_count`, and is sorted by source count descending — contracts appearing in more independent sources rank higher.

### Tips

- Start with `--sources bytecode` to see the strongest-signal "hardcoded reference" set cheaply, before committing Dune credits on the heavier queries.
- For popular target addresses (oracles, canonical tokens, well-known routers), use `--min-txs` to focus on actively-used integrators.
- If the target is typically stored in a `mapping(... => address)`, the linear storage verification will always miss it. Run with `--no-verify` to keep all candidates regardless.
- Use `--trace-workers 4` (or higher) to speed up the activity filter on providers that tolerate concurrent trace requests (Chainstack tracing tier, Erigon).
- For exact per-slot details on a small candidate list, rerun with `--verify-slots 200` to widen the storage scan.

### Limitations

- **Dune cost**: queries 2-4 scan 365 days of Ethereum `transactions`, `traces`, and `logs`. Running the full pipeline against a popular address can consume a meaningful fraction of a monthly Dune credit allowance. Query 1 (bytecode) is the cheapest and unbounded in time.
- **365-day time bound** on the runtime queries (tx, trace, event). References older than one year are invisible in those sources. The bytecode query has no time bound.
- **Mappings are invisible** to the linear storage scan. If the target is stored in `mapping(... => address)` across candidates, use `--no-verify`.
- **Proxy implementation addresses** at EIP-1967 slots (`impl` = `0x360894…`, `admin` = `0xb53127…`) are far outside the default 50-slot range. Proxies pointing at the target implementation will be dropped by verify unless `--no-verify` is used or `--verify-slots` is set absurdly high.
- **ERC-20/721/1155 standard functions excluded**: `transfer`, `approve`, `setApprovalForAll`, and the various `safeTransferFrom` variants are filtered out at the SQL level as counterparty noise. ERC-20 allowances to the target address are therefore treated as noise, not integration signal. Edit `dune_queries.sql` to include them if that's relevant.
- **Standard token events excluded** at the SQL level for the same reason: `Transfer`, `Approval`, `ApprovalForAll`, `TransferSingle`, `TransferBatch`.
- **Obfuscated references don't match**: XOR'd, hash-derived, or split-stored addresses pass through the bytecode scan undetected.
- **Activity filter requires a tracing RPC** (Erigon / OpenEthereum / Chainstack tracing tier / Alchemy trace add-on). Standard RPC is sufficient only for `--verify`.
- **Mainnet only**: SQL uses the `ethereum.*` tables; Etherscan URLs point at `etherscan.io`. Adapting to other chains requires updating both.
- **`storage_slots_matched` is a "latest"-state check**. A contract that once stored the address and later cleared it is correctly filtered out, but the annotation is not a historical audit.

Full per-query details and rationale are in the module-level docstring of `find_address_refs.py` and the header of `dune_queries.sql`.

---

## Shared module: `ethrpc/`

A small internal package used by Steps 1 and 4. It wraps Ethereum JSON-RPC primitives, block-range resolution with timestamp-accurate binary search, `trace_filter` support detection and chunked scanning, and bytecode/account classification (`[EOA]` / `[Contract]` / `[7702del]`). It has no dependencies beyond `requests`. You do not run it directly; it's imported by the two scripts that need it.

---

## Suggested Workflow

Run the four steps in order to build a comprehensive picture of who depends on the deprecation target:

```
Step 1  →  Identify active on-chain callers of the function (EOAs and contracts)
Step 2  →  From the contracts found, confirm which are verified integrators
            and cross-reference with contracts not already in Step 1 results
Step 3  →  Surface off-chain tooling, SDKs, and upcoming integrations
            not yet visible on-chain
Step 4  →  If the deprecation target is a CONTRACT ADDRESS (not just a
            function), find every deployed contract that references it
            in bytecode, state, or events — independent of whether the
            reference is currently in use
```

The output of Step 1 (a list of counterparty addresses) can inform the `--query` used in Step 2. The results of Step 2 (verified contract addresses and names) can inform GitHub search terms used in Step 3. Step 4 works independently of the others but pairs well with Step 1: a contract found to reference the target address in Step 4 can be run through Step 1 to see whether it's actively being called on-chain.
