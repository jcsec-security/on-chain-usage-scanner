-- =============================================================================
-- Dune queries for find_address_refs.py
-- =============================================================================
--
-- Save each of the four queries below as a separate saved query on Dune.
-- Each accepts two text parameters:
--   {{target_address_raw}}     e.g. 0x57891966931eb4bb6fb81430e6ce0a03aabde063
--                              (20 raw bytes, used to match bytecode)
--   {{target_address_padded}}  e.g. 0x00000000000000000000000057891966931eb4bb6fb81430e6ce0a03aabde063
--                              (32 bytes, left-zero-padded as in ABI encoding)
--
-- Record each query's ID from Dune's URL and edit DEFAULT_QUERY_IDS in
-- find_address_refs.py (or pass them via --query-* CLI flags).
--
-- WHY TWO FORMATS?
--   - Runtime bytecode contains addresses as raw 20-byte PUSH20 operands.
--   - Calldata, event data, and indexed topics encode addresses in the
--     32-byte ABI form (left-padded with 12 zero bytes).
--   So the bytecode query uses `_raw`, and the other three use `_padded`.
--
-- TIME BOUNDS
--   - Queries 2, 3, 4 are bounded to the last 365 days via `block_time`.
--     This is a cost control — full-history scans of `transactions`,
--     `traces`, and `logs` are expensive on Dune's free tier. References
--     older than one year will NOT be surfaced.
--   - Query 1 (bytecode) has NO time bound. Contracts deployed at any
--     point in Ethereum history are caught if the target is in their
--     creation code.
--   Adjust the INTERVAL expressions below if your Dune tier supports
--   deeper history or you're investigating a legacy integration.
--
-- CASE SENSITIVITY
--   DuneSQL hex literals are case-insensitive; stick to lowercase for
--   consistency. Addresses in the parameter values should also be
--   lowercase to avoid mismatches inside the app (find_address_refs.py
--   normalizes both).
-- =============================================================================


-- ---------------------------------------------------------------------------
-- QUERY 1: BYTECODE SCAN (constants, immutables, constructor args)
-- ---------------------------------------------------------------------------
-- Scans the creation code of every contract deployment on Ethereum. The
-- `creation_traces.code` column holds the INITCODE + RUNTIME CODE +
-- CONSTRUCTOR ARGS concatenated, which means this single query catches:
--
--   * `address constant FOO = 0x...;`   (compiled into runtime bytecode)
--   * `address immutable FOO;`          (patched into runtime bytecode at
--                                        deploy time by the compiler)
--   * `constructor(address foo)`        (the argument is appended to the
--                                        creation code tail as ABI-encoded)
--
-- CAVEATS:
--   - Obfuscated references (XOR'd, hash-derived, split across two halves)
--     will NOT match — the scan is a raw byte search.
--   - Coincidental matches inside the runtime bytecode are theoretically
--     possible but cryptographically unlikely (~1 in 2^160).
--   - CREATE2 factories that deploy many identical children: each child
--     deploy produces one row.
--   - No time bound.
-- ---------------------------------------------------------------------------
SELECT
  'bytecode' AS source,
  address AS contract_address,
  tx_hash
FROM ethereum.creation_traces
WHERE bytearray_position(code, {{target_address_raw}}) > 0
ORDER BY block_time DESC;


-- ---------------------------------------------------------------------------
-- QUERY 2: TX INPUT SCAN (external setter calls)
-- ---------------------------------------------------------------------------
-- Surfaces contracts that received a transaction whose calldata contains
-- the target address in ABI-encoded form. Catches externally-triggered
-- setters: setAdmin(X), setOracle(X), updateRouter(X), transferOwnership(X),
-- etc. — anything a user/admin calls externally while passing the target.
--
-- Excludes standard ERC-20/721/1155 function selectors (see below) because
-- these represent counterparty traffic rather than integration references:
--   0xa9059cbb  transfer(address,uint256)
--   0x23b872dd  transferFrom(address,address,uint256)
--   0x095ea7b3  approve(address,uint256)
--   0xa22cb465  setApprovalForAll(address,bool)
--   0x42842e0e  safeTransferFrom(address,address,uint256)           (ERC-721)
--   0xb88d4fde  safeTransferFrom(address,address,uint256,bytes)     (ERC-721)
--   0xf242432a  safeTransferFrom(address,address,uint256,uint256,bytes)
--                                                                    (ERC-1155)
--   0x2eb2c2d6  safeBatchTransferFrom(...)                            (ERC-1155)
--
-- NOTE: `approve()` technically writes the spender address to an allowances
-- mapping in the token contract's state. If you consider allowances to the
-- target address to be integration signals (e.g., investigating protocol
-- spenders), REMOVE 0x095ea7b3 from the exclusion list.
--
-- Also NOT excluded (may produce noise depending on use case):
--   - permit() / EIP-2612 — common in DeFi and may or may not be signal
--   - Uniswap v2/v3 swap/mint calls passing the target as a recipient
--   - WETH deposit/withdraw via router contracts
--
-- CAVEATS:
--   - 365-day time bound. References beyond that are invisible here.
--   - Matches calldata anywhere, including cases where the target is a
--     parameter to a subcall (e.g. multicall data).
--   - Does NOT catch internal setter calls — use Query 3 for that.
-- ---------------------------------------------------------------------------
SELECT
  'tx_input' AS source,
  "to" AS contract_address,
  hash AS tx_hash
FROM ethereum.transactions
WHERE bytearray_position(data, {{target_address_padded}}) > 0
  AND bytearray_length(data) >= 4
  AND bytearray_substring(data, 1, 4) NOT IN (
    0xa9059cbb, 0x23b872dd, 0x095ea7b3, 0xa22cb465,
    0x42842e0e, 0xb88d4fde, 0xf242432a, 0x2eb2c2d6
  )
  AND block_time > NOW() - INTERVAL '365' DAY
ORDER BY block_time DESC;


-- ---------------------------------------------------------------------------
-- QUERY 3: INTERNAL TRACE SCAN (contract-to-contract calls)
-- ---------------------------------------------------------------------------
-- Surfaces contracts that were called INTERNALLY (from another contract)
-- with the target address in the call's input data. Catches cases like:
--   - A factory deploying a contract then calling initialize(target)
--   - A router/proxy forwarding a call with the target as an argument
--   - A governance contract executing a setter on a managed contract
--
-- Same ERC-20/721/1155 selector exclusion as Query 2 applies here — and
-- for the same reason. See Query 2 for the full list and rationale.
--
-- CAVEATS:
--   - 365-day time bound.
--   - `call_type = 'call'` filters to regular internal calls. Static and
--     delegatecalls are excluded, since they don't write state on the
--     callee. (If you need delegate or static calls, broaden the predicate.)
--   - Each internal call frame is its own row. A single tx may produce
--     multiple rows per contract. The Python driver dedupes by
--     (contract_address, tx_hash) downstream.
-- ---------------------------------------------------------------------------
SELECT
  'trace_input' AS source,
  "to" AS contract_address,
  tx_hash
FROM ethereum.traces
WHERE call_type = 'call'
  AND bytearray_length(input) >= 4
  AND bytearray_substring(input, 1, 4) NOT IN (
    0xa9059cbb, 0x23b872dd, 0x095ea7b3, 0xa22cb465,
    0x42842e0e, 0xb88d4fde, 0xf242432a, 0x2eb2c2d6
  )
  AND bytearray_position(input, {{target_address_padded}}) > 0
  AND block_time > NOW() - INTERVAL '365' DAY
ORDER BY block_time DESC;


-- ---------------------------------------------------------------------------
-- QUERY 4: EVENT LOG SCAN (state-change events)
-- ---------------------------------------------------------------------------
-- Surfaces contracts that emitted an event mentioning the target address.
-- Strong signal: setter functions almost always emit an event like
-- `AdminChanged(address)`, `OracleUpdated(address)`, `RoleGranted(...)`,
-- etc. If the target was ever assigned to a role/config in this contract,
-- this query typically catches it.
--
-- The target is checked against:
--   - topic1 / topic2 / topic3  (indexed event parameters)
--   - non-indexed `data` field  (substring search)
--
-- Excludes standard token events by topic0 (these represent counterparty
-- traffic, not integration):
--   Transfer            0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef
--   Approval            0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925
--   ApprovalForAll      0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31
--   TransferSingle      0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62
--   TransferBatch       0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb
--
-- CAVEATS:
--   - 365-day time bound.
--   - A contract that emits a setter event and THEN later overwrites the
--     value will still appear here. Use the storage verification step in
--     the Python driver (default on) to filter out stale references.
--   - `contract_address` is the EMITTER of the event — the contract that
--     holds the state — not necessarily the subject of the event.
-- ---------------------------------------------------------------------------
SELECT
  'event_log' AS source,
  contract_address,
  tx_hash
FROM ethereum.logs
WHERE topic0 NOT IN (
    0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef,
    0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925,
    0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31,
    0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62,
    0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb
  )
  AND (
    topic1 = {{target_address_padded}}
    OR topic2 = {{target_address_padded}}
    OR topic3 = {{target_address_padded}}
    OR bytearray_position(data, {{target_address_padded}}) > 0
  )
  AND block_time > NOW() - INTERVAL '365' DAY
ORDER BY block_time DESC;
