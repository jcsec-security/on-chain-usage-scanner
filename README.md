# Deprecated Function Usage Research Toolkit

A set of three scripts to systematically research the real-world usage of a contract or specific functions that are planned for deprecation. The three steps are designed to be run in sequence, each answering a different dimension of the research question.

```
Step 1 — Who calls it on-chain?          on_chain_target_interactions.py
Step 2 — Who has integrated it in code?  etherscan_verified_contracts_search.py
Step 3 — Who references it on GitHub?    github_code_search.py
```

---

## Requirements

```bash
pip install requests eth-utils beautifulsoup4 python-dateutil tqdm
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
| `--signature` | ✅ | Canonical function signature (used to derive the 4-byte selector) |
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
  --min-txs 5 \
  --months 3
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
  --min-stars 50 \
  --min-activity 90d \
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

## Suggested Workflow

Run the three steps in order to build a comprehensive picture of who depends on the function being deprecated:

```
Step 1  →  Identify active on-chain callers (EOAs and contracts)
Step 2  →  From the contracts found, confirm which are verified integrators
            and cross-reference with contracts not already in Step 1 results
Step 3  →  Surface off-chain tooling, SDKs, and upcoming integrations
            not yet visible on-chain
```

The output of Step 1 (a list of counterparty addresses) can inform the `--query` used in Step 2. The results of Step 2 (verified contract addresses and names) can inform GitHub search terms used in Step 3.