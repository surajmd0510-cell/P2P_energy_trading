"""Offline end-to-end check: real trading_engine -> ledger -> settlement -> (fake) PayPal.
Run: python smoke_test.py   (needs numpy, paho-mqtt, flask, requests; no broker, no PayPal account)"""
import json, os, subprocess, sys, tempfile
here = os.path.dirname(os.path.abspath(__file__))
work = tempfile.mkdtemp()
os.chdir(work)
env = dict(os.environ, ENGINE_BALANCE_CHECK="0")
subprocess.run([sys.executable, "-c", f"""
import sys, io, contextlib, time; sys.path.insert(0, {here!r})
import trading_engine as E
E.get_nasa_solar_generation = lambda h: 6 if 6 <= h < 18 else 0
time.sleep = lambda s: None
E.TEST_OVERRIDE_HOUR = 13
class C:
    def publish(self, *a, **k): pass
for _ in range(40):
    with contextlib.redirect_stdout(io.StringIO()): E.match_and_trade(C())
"""], check=True, env=env)

sys.path.insert(0, here)
os.environ.update(REGION="US", AUTO_APPROVE_LIMIT="2.00", LEDGER_VERIFY="full",
                  DB_PATH=os.path.join(work, "t.db"), LEDGER_PATH=os.path.join(work, "blockchain_ledger.json"),
                  MEMBERS_FILE=os.path.join(work, "members.json"))
json.dump({"house1": {"email": "a@x.com", "role": "prosumer"}, "house2": {"email": "b@x.com", "role": "prosumer"},
           "house3": {"email": "c@x.com", "role": "consumer"}, "house4": {"email": "d@x.com", "role": "consumer"}},
          open(os.environ["MEMBERS_FILE"], "w"))
import store as S, settlement as T, paypal_client as P


class Fake:
    sent = []
    def payout(self, bid, cur, items): Fake.sent.append(items); return "PP-" + bid
    def get_payout(self, pid):
        return {"items": [{"transaction_status": "SUCCESS", "payout_item": {"sender_item_id": i["id"]}}
                          for its in Fake.sent for i in its]}


P._pp = Fake(); T.pp = lambda: P._pp
TOPUP = 100000  # 1000.00 per consumer, as if captured via PayPal Checkout
with S.tx() as c:
    for m in ("house3", "house4"):
        c.execute("UPDATE members SET balance=? WHERE id=?", (TOPUP, m))
        c.execute("INSERT INTO orders VALUES(?,?,?,?,?,0)", ("O-" + m, m, "wallet_topup", TOPUP, "captured"))
print("cycle 1:", T.run_cycle())
b = S.q("SELECT id,status,total FROM batches")[0]
print("batch:", b)
assert b["status"] == "pending_approval", "batch above the auto-approve limit must wait for a human"
print("approve ->", T.submit(b["id"], "human")); T.refresh_open()
held = S.scalar("SELECT SUM(balance+accrued) FROM members")
paid = S.scalar("SELECT SUM(amount) FROM items WHERE status='paid'")
print("held", held, "paid", paid, "| books balance:", 2 * TOPUP == held + paid)
assert 2 * TOPUP == held + paid and S.scalar("SELECT status FROM batches") == "settled"
d = json.load(open(os.environ["LEDGER_PATH"])); d[3]["energy_quantity_kw"] = 99
json.dump(d, open(os.environ["LEDGER_PATH"], "w"))
print("tampered ledger:", T.run_cycle())
assert T.run_cycle().get("blocked") == "ledger_tamper"
print("ALL CHECKS PASSED")
