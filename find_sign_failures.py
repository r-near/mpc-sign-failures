# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "tqdm"]
# ///
"""Find failed sign() calls on v1.signer via FastNEAR transactions API."""

import argparse
import json
import sys
from collections import Counter

import requests
from tqdm import tqdm

API = "https://tx.main.fastnear.com"
ACCOUNT = "v1.signer"


def list_failed(limit_total: int, before_block: int | None) -> list[dict]:
    """Page /v0/account, collecting failed function-call rows."""
    rows: list[dict] = []
    body = {
        "account_id": ACCOUNT,
        "desc": True,
        "is_real_receiver": True,
        "is_function_call": True,
        "is_success": False,
        "limit": 200,
    }
    if before_block is not None:
        body["to_tx_block_height"] = before_block
    bar = tqdm(total=limit_total, desc="listing failed txs", unit="tx")
    resume = None
    while len(rows) < limit_total:
        if resume:
            body["resume_token"] = resume
        r = requests.post(f"{API}/v0/account", json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        batch = data.get("account_txs", [])
        if not batch:
            break
        rows.extend(batch)
        bar.update(len(batch))
        resume = data.get("resume_token")
        if not resume:
            break
    bar.close()
    return rows[:limit_total]


def resolve(hashes: list[str]) -> list[dict]:
    """Batch-fetch transaction details, 20 hashes at a time."""
    out: list[dict] = []
    bar = tqdm(total=len(hashes), desc="resolving tx detail", unit="tx")
    for i in range(0, len(hashes), 20):
        chunk = hashes[i : i + 20]
        r = requests.post(f"{API}/v0/transactions", json={"tx_hashes": chunk}, timeout=60)
        r.raise_for_status()
        out.extend(r.json().get("transactions", []))
        bar.update(len(chunk))
    bar.close()
    return out


def _methods(receipt_inner: dict) -> list[str]:
    if "Action" not in receipt_inner:
        return []
    return [
        a["FunctionCall"]["method_name"]
        for a in receipt_inner["Action"].get("actions", [])
        if "FunctionCall" in a
    ]


def _fmt_err(fail: dict) -> str:
    try:
        return fail["ActionError"]["kind"]["FunctionCallError"]["ExecutionError"]
    except (KeyError, TypeError):
        return json.dumps(fail)[:200]


def classify(tx: dict) -> dict | None:
    """Return a record if this tx involved a call to v1.signer.sign() and at
    least one receipt in the tx ended in Failure. Walks every receipt in the
    chain — the failing receipt is often on the caller's contract (e.g. the
    bridge's sign callback panicking on a stale promise resume)."""
    tx_hash = tx["execution_outcome"]["id"]
    caller = tx["transaction"]["signer_id"]
    direct_target = tx["transaction"]["receiver_id"]

    called_sign = False
    failures: list[dict] = []
    for r in tx.get("receipts", []):
        receiver = r["receipt"]["receiver_id"]
        inner = r["receipt"]["receipt"]
        methods = _methods(inner)
        if receiver == ACCOUNT and "sign" in methods:
            called_sign = True
        status = r["execution_outcome"]["outcome"].get("status", {})
        if isinstance(status, dict) and "Failure" in status:
            failures.append(
                {
                    "receiver": receiver,
                    "methods": methods,
                    "error": _fmt_err(status["Failure"]),
                }
            )

    if not called_sign or not failures:
        return None
    return {
        "tx_hash": tx_hash,
        "caller": caller,
        "entry_contract": direct_target,
        "failures": failures,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=400, help="max failed txs to list")
    p.add_argument(
        "--before-block",
        type=int,
        default=None,
        help="exclusive upper bound block height (use to skip recent pending)",
    )
    p.add_argument("--resolve-cap", type=int, default=400, help="max hashes to resolve")
    args = p.parse_args()

    print(f"step 1: list up to {args.limit} failed function-call txs touching {ACCOUNT}")
    rows = list_failed(args.limit, args.before_block)
    print(f"  → {len(rows)} candidate rows")
    if not rows:
        return

    hashes = [r["transaction_hash"] for r in rows][: args.resolve_cap]
    print(f"step 2: resolve {len(hashes)} tx details")
    txs = resolve(hashes)
    print(f"  → {len(txs)} txs returned")

    print("step 3: classify")
    hits: list[dict] = []
    for tx in tqdm(txs, desc="classifying", unit="tx"):
        h = classify(tx)
        if h:
            hits.append(h)

    print(f"\n=== {len(hits)} sign-related failures out of {len(txs)} resolved txs ===\n")
    if not hits:
        return

    # Group by (failing-receiver, failing-method, error). One tx can contribute
    # multiple failures; we count each receipt-level failure.
    grouped: Counter[tuple[str, str, str]] = Counter()
    for h in hits:
        for f in h["failures"]:
            key = (f["receiver"], ",".join(f["methods"]) or "<no fc>", f["error"])
            grouped[key] += 1

    print("Top failing receipts (receiver / method / error):")
    for (recv, meth, err), n in grouped.most_common(15):
        print(f"  [{n:4d}]  {recv}  ::  {meth}")
        print(f"          → {err[:140]}")

    print("\nEntry-contract distribution (where the user's tx landed first):")
    for entry, n in Counter(h["entry_contract"] for h in hits).most_common(10):
        print(f"  [{n:4d}]  {entry}")

    print("\nFirst 5 hits (one line per failing receipt):")
    for h in hits[:5]:
        print(f"  tx {h['tx_hash']}  caller={h['caller']}  entry={h['entry_contract']}")
        for f in h["failures"]:
            print(f"     FAIL  {f['receiver']}  ::  {','.join(f['methods']) or '<no fc>'}")
            print(f"       → {f['error'][:160]}")


if __name__ == "__main__":
    main()
