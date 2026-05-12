# mpc-sign-failures

Scan historic failing `sign()` calls on the NEAR `v1.signer` MPC contract.

Block explorers don't make it easy to filter cross-contract MPC failures by method and outcome — this script does. It uses the [FastNEAR transactions API](https://docs.fastnear.com/tx) to list transactions where `v1.signer` was the real receiver of a function call and the transaction did not succeed, then resolves each one and walks the receipt graph to surface the actual failing receipt (which is usually on the *caller's* contract — e.g. a bridge or relayer panicking in its sign callback).

## Usage

Requires [`uv`](https://docs.astral.sh/uv/). The script declares its own deps inline (PEP 723).

```bash
uv run find_sign_failures.py --limit 400 --before-block 198053128
```

Flags:

- `--limit N` — max number of failed candidate txs to list (default 400)
- `--resolve-cap N` — max number of candidates to fetch full detail for (default 400)
- `--before-block H` — exclusive upper bound on tx block height. Set this a few hours behind the chain head; otherwise a chunk of results will be still-pending yielded promises rather than real failures.
- `--api-key KEY` — optional FastNEAR API key (or set `FASTNEAR_API_KEY`). Public access works but is rate-limited; you'll hit 429s past ~250 hashes through `/v0/transactions`. Get a key at [dashboard.fastnear.com](https://dashboard.fastnear.com).

## Speed

End-to-end throughput is roughly linear and bottlenecked by `/v0/transactions` (resolve step ~50–60 tx/s; listing is ~300 tx/s).

| `--limit` | wall time | end-to-end rate |
| ---: | ---: | ---: |
|   400 |  6.9s | ~58 tx/s |
|  1000 | 16.7s | ~60 tx/s |
|  2000 | 35.2s | ~57 tx/s |

Numbers above are with an API key. Without one, the public tier 429s during resolve.

## How it works

1. `POST /v0/account` with `account_id=v1.signer`, `is_real_receiver=true`, `is_function_call=true`, `is_success=false`. Pages with `resume_token`.
2. `POST /v0/transactions` in batches of 20 hashes to fetch full receipt detail.
3. For each tx, keep it iff there's a `v1.signer` receipt with `method_name="sign"` **and** at least one `Failure` receipt anywhere in the chain. Group results by `(failing_receiver, failing_method, error_message)`.

Step 3 is what makes the output useful — on NEAR's promise-yield model the `sign()` receipt itself almost always succeeds; the failure lands on whichever contract handles the sign callback.

## Caveats

- `is_success: false` means "failed **or** pending" per the FastNEAR docs. Use `--before-block` to skip the still-pending tail.
- Only catches transactions that actually called `v1.signer.sign`. It deliberately does not surface MPC-node `respond` panics (those have no `sign` receipt) — those are a separate operator-side concern.
- FastNEAR `/v0/transactions` returns a tx only once it's indexed; very recent hashes may come back missing from the response.

## Output shape

```
=== 15 sign-related failures out of 400 resolved txs ===

Top failing receipts (receiver / method / error):
  [  15]  btc-connector.bridge.near  ::  sign_btc_transaction_callback
          → Smart contract panicked: Already signed

Entry-contract distribution (where the user's tx landed first):
  [  15]  btc-connector.bridge.near
```
