"""Writes a sample blockchain_ledger.json in the exact format trading_engine.py produces (patched hashing),
so you can demo the PayPal layer without the engine running."""
import datetime, hashlib, json, random, sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
chain, prev = [], "0" * 32
for h in range(1, n + 1):
    s, b = random.choice(["house1", "house2"]), random.choice(["house3", "house4"])
    amt, price = random.choice([1, 2, 3]), round(random.uniform(7.10, 7.60), 2)
    ts = str(datetime.datetime.now())
    cur = hashlib.sha256(f"{prev}{ts}{s}{b}{amt}{price}".encode()).hexdigest()
    chain.append({"block_height": h, "previous_block_hash": prev, "current_block_hash": cur, "timestamp": ts,
                  "seller_id": s, "buyer_id": b, "clearing_price_per_kwh": price, "energy_quantity_kw": amt,
                  "total_cost_inr": round(amt * price, 2),
                  "status": random.choice(["SUCCESS"] * 49 + ["REVERTED_DELIVERY_FAILURE"])})
    prev = cur
json.dump(chain, open("blockchain_ledger.json", "w"), indent=4)
print("wrote", n, "blocks")
