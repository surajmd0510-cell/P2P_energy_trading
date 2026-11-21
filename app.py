"""SolarPool: PayPal funding + guarded energy-payout service. Run: python app.py"""
import os, threading, time
from decimal import Decimal
from flask import Flask, jsonify, redirect, request, send_from_directory
import store as S, settlement as T
from paypal_client import pp, PayPalError

app = Flask(__name__, static_folder="static")
BASE = os.environ.get("PUBLIC_URL", "http://localhost:5000")


@app.get("/")
def home():
    return send_from_directory("static", "console.html")


@app.post("/api/topup")  # purpose: wallet_topup (buyer funds) | project_fund (community solar pool)
def topup():
    j = request.get_json(force=True)
    mid, purpose = j.get("member_id"), j.get("purpose", "wallet_topup")
    minor = int(Decimal(str(j.get("amount", 0))) * 100)
    if purpose not in ("wallet_topup", "project_fund") or minor < 100 or minor > 100000 \
            or not S.scalar("SELECT 1 FROM members WHERE id=?", (mid,)):
        return jsonify(error="invalid member, purpose or amount (1-1000)"), 400
    ref = f"{mid}-{purpose}-{int(time.time())}"
    try:
        oid, url = pp().create_order(minor, S.CURRENCY, ref, f"{BASE}/paypal/return", f"{BASE}/?cancelled=1")
    except PayPalError as e:
        return jsonify(error=str(e)[:300]), 502
    with S.tx() as c:
        c.execute("INSERT INTO orders VALUES(?,?,?,?,'created',?)", (oid, mid, purpose, minor, time.time()))
    return jsonify(order_id=oid, approve_url=url)


@app.get("/paypal/return")
def paypal_return():
    oid = request.args.get("token", "")
    o = S.q("SELECT * FROM orders WHERE order_id=? AND status='created'", (oid,))
    if not o:
        return redirect("/?topup=unknown")
    try:
        done = pp().capture_order(oid)
    except PayPalError:
        return redirect("/?topup=failed")
    if done:
        with S.tx() as c:
            c.execute("UPDATE orders SET status='captured' WHERE order_id=? AND status='created'", (oid,))
            if o[0]["purpose"] == "wallet_topup":
                c.execute("UPDATE members SET balance=balance+? WHERE id=?", (o[0]["amount"], o[0]["member_id"]))
    return redirect("/?topup=ok" if done else "/?topup=failed")


@app.post("/api/settle")
def settle_now():
    return jsonify(T.run_cycle())


@app.post("/api/batches/<bid>/<action>")
def decide(bid, action):
    if action == "approve":
        return jsonify(status=T.submit(bid, "human"))
    if action == "reject":
        T.reject(bid)
        return jsonify(status="rejected")
    return jsonify(error="unknown action"), 400


@app.post("/api/webhooks/paypal")
def webhook():
    ev = request.get_json(force=True)
    if os.environ.get("PAYPAL_WEBHOOK_ID"):
        try:
            if not pp().verify_webhook(request.headers, ev):
                return "", 400
        except PayPalError:
            return "", 400
    if str(ev.get("event_type", "")).startswith("PAYMENT.PAYOUT"):
        T.refresh_open()
    return "", 200


@app.get("/api/state")
def state():
    cap = S.scalar("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='captured' AND purpose='wallet_topup'")
    held = S.scalar("SELECT COALESCE(SUM(balance+accrued),0) FROM members")
    flight = S.scalar("SELECT COALESCE(SUM(amount),0) FROM items WHERE status IN ('sent','unclaimed')")
    paid = S.scalar("SELECT COALESCE(SUM(amount),0) FROM items WHERE status='paid'")
    batches = S.q("SELECT * FROM batches ORDER BY created DESC LIMIT 8")
    for b in batches:
        b["items"] = S.q("SELECT member_id,amount,status FROM items WHERE batch_id=?", (b["id"],))
    return jsonify(
        region=S.REGION, currency=S.CURRENCY, members=S.q("SELECT * FROM members ORDER BY id"),
        batches=batches, funded=S.scalar("SELECT COALESCE(SUM(amount),0) FROM orders "
                                         "WHERE status='captured' AND purpose='project_fund'"),
        trades=S.scalar("SELECT COUNT(*) FROM trades WHERE status='applied'"),
        recon=dict(captured=cap, held=held, in_flight=flight, paid=paid, ok=cap == held + flight + paid))


def scheduler():
    while True:
        time.sleep(S.CFG["interval"])
        try:
            T.run_cycle()
        except Exception as e:  # keep the loop alive; the console shows state either way
            print("settlement cycle error:", e)


if __name__ == "__main__":
    S.db()
    if os.environ.get("AUTO_SETTLE", "1") == "1":
        threading.Thread(target=scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
