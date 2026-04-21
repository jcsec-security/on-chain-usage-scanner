-- Dune queries for find_address_refs.py. See README.md for the full
-- description of what each query catches and the pipeline's limitations.
--
-- Save each query below separately on Dune with these Text parameters:
--   target_address_raw      (20 bytes, no padding — e.g. 0x5789...aabde063)
--   target_address_padded   (32 bytes, left-zero-padded ABI form)
-- Note each saved query's ID and paste them into DEFAULT_QUERY_IDS in
-- find_address_refs.py.
--
-- Engine: DuneSQL. bytearray_* functions do not exist on the legacy Spark
-- engine.


-- -------- Query 1: bytecode (constants, immutables, constructor args) ------
-- No time bound: catches deployments from any point in Ethereum history.
SELECT
  'bytecode' AS source,
  address AS contract_address,
  tx_hash
FROM ethereum.creation_traces
WHERE bytearray_position(code, {{target_address_raw}}) > 0
ORDER BY block_time DESC;


-- -------- Query 2: tx input (external setter calls) ------------------------
-- Excluded ERC-20/721/1155 standard function selectors:
--   0xa9059cbb  transfer(address,uint256)
--   0x23b872dd  transferFrom(address,address,uint256)
--   0x095ea7b3  approve(address,uint256)
--   0xa22cb465  setApprovalForAll(address,bool)
--   0x42842e0e  safeTransferFrom(address,address,uint256)              (ERC-721)
--   0xb88d4fde  safeTransferFrom(address,address,uint256,bytes)        (ERC-721)
--   0xf242432a  safeTransferFrom(address,address,uint256,uint256,bytes) (ERC-1155)
--   0x2eb2c2d6  safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)
-- Remove 0x095ea7b3 from the NOT IN list if you want ERC-20 allowances
-- to the target address included as signal rather than noise.
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


-- -------- Query 3: internal traces (contract-to-contract calls) ------------
-- Same selector exclusions as Query 2. call_type = 'call' excludes
-- delegatecall and staticcall; broaden if you need those.
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


-- -------- Query 4: event logs (state-change events) ------------------------
-- Excluded standard token event topic0s (same rationale as Query 2):
--   Transfer           0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef
--   Approval           0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925
--   ApprovalForAll     0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31
--   TransferSingle     0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62
--   TransferBatch      0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb
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
