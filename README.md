# RPC Function Usage Scanner

A lightweight Python script to analyze **who is calling a specific function on a smart contract** over the last *N days*, including **internal contract calls**.

The scanner uses **trace RPC APIs** to detect both direct transactions and internal calls within the same transaction.

---

# Features

* Detects **direct and internal function calls**
* Works without Etherscan or external indexers
* Identifies **unique counterparties**
* Classifies addresses as:

  * `[EOA]` — externally owned account
  * `[Contract]` — smart contract
  * `[7702del]` — EIP-7702 delegated account
* Aggregates **number of transactions per caller**

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
| `--timeout`              | RPC timeout seconds                                |
| `--full-address`         | Print full addresses instead of shortened          |
| `--verbose-trace-errors` | Show trace RPC errors                              |

---

# Output Format

Example output:

```
# Contract: 0x32400084C286CF3E17e7B677ea9583e60a000324
# Function signature: requestL2Transaction(address,uint256,bytes,uint256,uint256,bytes[],address)
# Selector: 0xdd9a13f
# Lookback days: 14
# Start block: 21938450
# End block: 21972031
# Trace frames seen: 421
# Selector-matching frames: 23
# Matched txs: 17
# Unique counterparties: 6

[EOA] 0x1234....ab (5 txs)
[Contract] 0x98cd....ff (4 txs)
[EOA] 0x77aa....12 (3 txs)
```

Each result line shows:

```
[TYPE] ADDRESS (n txs)
```

Where:

* **TYPE**

  * `[EOA]` externally owned account
  * `[Contract]` smart contract
  * `[7702del]` EIP-7702 delegated account

* **n txs** = number of **unique transactions** where the address invoked the function.

If the same address calls the function multiple times in the same transaction, it counts as **1 transaction**.

---

# Limitations

* Requires RPC providers exposing **trace APIs (`trace_filter`)**.
* Some RPC providers (Infura, Alchemy, Cloudflare) **do not support tracing**.
* Very large time windows may require adjusting `--chunk-size`.
* `(n txs)` counts **unique transactions**, not individual call frames.
* The script assumes **Ethereum-style tracing APIs** (Erigon/OpenEthereum/Nethermind).

---

# How It Works

1. Verify RPC supports `trace_filter`
2. Verify target address contains code
3. Convert lookback days → block range
4. Query traces using:

```
trace_filter(toAddress=[target_contract])
```

5. Filter frames where:

```
call.input.startswith(function_selector)
```

6. Attribute caller:

   * **internal call** → `action.from`
   * **direct tx** → `tx.from`
7. Classify caller via `eth_getCode`
8. Aggregate results by unique transaction hash

---

# License

MIT

---



python usage_scanner.py \
  --address 0x32400084c286cf3e17e7b677ea9583e60a000324 \
  --signature "requestL2Transaction(address,uint256,bytes,uint256[],address)" \
  --days 14 \
  --apikey N32PW1UV4GDD76ZC5199I8WUSGADM7D59F \
  --rpc-url "https://ethereum-mainnet.core.chainstack.com/0c589fd97722cca4bc8b35ef7781b396" \
  --chainid 1 \
  --verbose-trace-errors 
 
python usage_scanner.py \
  --address 0x32400084c286cf3e17e7b677ea9583e60a000324 \
  --signature "finalizeEthWithdrawal(uint256,uint256,uint16,bytes,bytes32[])" \
  --days 14 \
  --apikey N32PW1UV4GDD76ZC5199I8WUSGADM7D59F \
  --rpc-url "https://ethereum-mainnet.core.chainstack.com/0c589fd97722cca4bc8b35ef7781b396" \
  --chainid 1 \
  --verbose-trace-errors # on-chain-usage-scanner

