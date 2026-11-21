"""Ledger -> wallets -> guarded PayPal payouts. The engine decides prices; this layer moves money."""
import hashlib, json, os, time
from decimal import Decimal
import store as S
from paypal_client import pp, PayPalError

PAPER_SELL, PAPER_BUY = Decimal("4.50"), Decimal("9.20")  # BESCOM band the engine clears inside
OK_STATUS = {"SUCCESS", "DELIVERED", "OK"}
# Adapter: field names as written by trading_engine.py (blockchain_ledger.json).
KEYS = {"height": ("block_height", "height", "index"), "seller": ("seller_id", "seller"),
        "buyer": ("buyer_id", "buyer"), "volume": ("energy_quantity_kw", "volume", "V"),
        "price": ("clearing_price_per_kwh", "clearing_price", "price"), "status": ("status", "delivery_status"),
        "hash": ("current_block_hash", "hash")}


def _get(b, names, default=None):
    return next((b[n] for n in names if n in b), default)


def read_blocks():
    path = S.CFG["ledger"]
    if not os.path.exists(path):
        return [], []
    data = json.load(open(path))
    raw = data.get("chain", data.get("blocks", [])) if isinstance(data, dict) else data
    out = []
    for i, b in enumerate(raw):
        s, bu = _get(b, KEYS["seller"]), _get(b, KEYS["buyer"])
        if not s or not bu:
            continue  # genesis block
        out.append(dict(key=str(_get(b, KEYS["hash"], i)), height=int(_get(b, KEYS["height"], i)), seller=str(s), buyer=str(bu),
                        volume=float(_get(b, KEYS["volume"], 0)), price=float(_get(b, KEYS["price"], 0)),
                        ok=str(_get(b, KEYS["status"], "SUCCESS")).upper() in OK_STATUS))
    return out, raw


def chain_ok(raw):
    """Refuse to pay out on a tampered ledger. links = prev/current hash chain; full = also recompute each hash
    exactly as trading_engine.py does (needs the patched engine that hashes the stored, rounded values)."""
    mode = S.CFG["verify"]
    for i in range(1, len(raw)) if mode != "off" else []:
        a, b = raw[i - 1], raw[i]
        if b.get("previous_block_hash") != a.get("current_block_hash"):
            return False
        if mode == "full":
            s = (f"{b['previous_block_hash']}{b['timestamp']}{b['seller_id']}{b['buyer_id']}"
                 f"{b['energy_quantity_kw']}{b['clearing_price_per_kwh']}")
            if hashlib.sha256(s.encode()).hexdigest() != b["current_block_hash"]:
                return False
    return True


def local_price(p_star):
    """Map the engine's INR clearing price onto the region's tariff band, keeping the NBS surplus split."""
    t = min(max((Decimal(str(p_star)) - PAPER_SELL) / (PAPER_BUY - PAPER_SELL), Decimal(0)), Decimal(1))
    return S.R["sell"] + t * (S.R["buy"] - S.R["sell"])


def ingest():
    blocks, raw = read_blocks()
    if not chain_ok(raw):
        return {"blocked": "ledger_tamper"}
    with S.tx() as c:
        members = {r["id"] for r in c.execute("SELECT id FROM members")}
        known = {r["key"] for r in c.execute("SELECT key FROM trades")}
        for b in blocks:
            if b["key"] in known or not b["ok"] or b["seller"] not in members \
               or b["buyer"] not in members or b["seller"] == b["buyer"]:
                continue  # skips reverted deliveries and P2G fallback blocks
            amt = int((Decimal(str(b["volume"])) * local_price(b["price"]) * 100).to_integral_value())
            c.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,'new')",
                      (b["key"], b["height"], b["seller"], b["buyer"], b["volume"], amt))
        for t in c.execute("SELECT * FROM trades WHERE status IN ('new','unfunded') ORDER BY rowid").fetchall():
            bal = c.execute("SELECT balance FROM members WHERE id=?", (t["buyer"],)).fetchone()["balance"]
            if bal >= t["amount"]:
                c.execute("UPDATE members SET balance=balance-? WHERE id=?", (t["amount"], t["buyer"]))
                c.execute("UPDATE members SET accrued=accrued+? WHERE id=?", (t["amount"], t["seller"]))
                c.execute("UPDATE trades SET status='applied' WHERE key=?", (t["key"],))
            else:
                c.execute("UPDATE trades SET status='unfunded' WHERE key=?", (t["key"],))
    return {"unfunded": S.scalar("SELECT COUNT(*) FROM trades WHERE status='unfunded'")}


# ---- Settlement agent: deterministic guardrails + LLM explanation ----------------------------
def review(items):
    risk, total = [], sum(i["amount"] for i in items)
    for i in items:
        past = [h["amount"] for h in S.q("SELECT amount FROM items WHERE member_id=? AND status IN ('sent','paid')",
                                         (i["member"],))]
        if i["amount"] > S.CFG["cap_payout"]:
            risk.append(f"payout_over_cap:{i['member']}")
        if not past and i["amount"] > S.CFG["first_limit"]:
            risk.append(f"large_first_payout:{i['member']}")
        elif len(past) >= 2 and i["amount"] > 3 * sum(past) / len(past):
            risk.append(f"spike_vs_history:{i['member']}")
    if total > S.CFG["cap_batch"]:
        risk.append("batch_over_cap")
    auto = not risk and total <= S.CFG["auto_limit"]
    return auto, risk, total


def explain(items, risk, total):
    facts = {"currency": S.CURRENCY, "total": S.money(total), "risk_flags": risk,
             "payouts": [{"member": i["member"], "amount": S.money(i["amount"])} for i in items]}
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            m = anthropic.Anthropic().messages.create(
                model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"), max_tokens=250,
                system="You write a 3-sentence plain-language note for a community energy co-op treasurer "
                       "about a proposed payout batch. Use ONLY the facts given and mention every risk flag. "
                       "Never suggest changing amounts. A human decides.",
                messages=[{"role": "user", "content": json.dumps(facts)}])
            return m.content[0].text.strip()
        except Exception:
            pass
    flags = ", ".join(risk) if risk else "no risk flags"
    return (f"Proposed batch pays {len(items)} member(s) {S.CURRENCY} {S.money(total)} for energy "
            f"sold to neighbours since the last settlement. Checks: {flags}.")


# ---- Batch lifecycle ---------------------------------------------------------------------------
def run_cycle():
    res = ingest()
    refresh_open()
    if res.get("blocked"):
        return res
    if S.q("SELECT 1 FROM batches WHERE status='pending_approval'"):
        return {**res, "note": "awaiting human approval"}
    items = [dict(member=r["id"], email=r["email"], amount=r["accrued"]) for r in
             S.q("SELECT id,email,accrued FROM members WHERE accrued>=? AND email<>'' ORDER BY id",
                 (S.CFG["min_payout"],))]
    if not items:
        return {**res, "note": "nothing to settle"}
    auto, risk, total = review(items)
    bid = f"SP-{S.REGION}-{int(time.time())}"
    with S.tx() as c:
        c.execute("INSERT INTO batches(id,total,status,risk,explanation,created) VALUES(?,?,?,?,?,?)",
                  (bid, total, "approved" if auto else "pending_approval", json.dumps(risk),
                   explain(items, risk, total), time.time()))
        c.executemany("INSERT INTO items VALUES(?,?,?,?,'queued')",
                      [(bid, i["member"], i["email"], i["amount"]) for i in items])
    if auto:
        submit(bid, "agent")
    return {**res, "batch": bid, "auto": auto}


def submit(bid, by):
    b = S.q("SELECT * FROM batches WHERE id=?", (bid,))[0]
    if b["status"] not in ("pending_approval", "approved", "submit_failed"):
        return b["status"]
    items = S.q("SELECT * FROM items WHERE batch_id=?", (bid,))
    for i in items:  # accruals may have changed while the batch waited
        if S.scalar("SELECT accrued FROM members WHERE id=?", (i["member_id"],)) < i["amount"]:
            with S.tx() as c:
                c.execute("UPDATE batches SET status='stale' WHERE id=?", (bid,))
            return "stale"
    try:
        pp_id = pp().payout(bid, S.CURRENCY, [dict(id=f"{bid}:{i['member_id']}", email=i["email"],
                                                  amount=i["amount"]) for i in items])
    except PayPalError as e:
        with S.tx() as c:
            c.execute("UPDATE batches SET status='submit_failed', explanation=explanation||? WHERE id=?",
                      (f" [PayPal error: {str(e)[:200]}]", bid))
        return "submit_failed"
    with S.tx() as c:
        for i in items:
            c.execute("UPDATE members SET accrued=accrued-? WHERE id=?", (i["amount"], i["member_id"]))
        c.execute("UPDATE items SET status='sent' WHERE batch_id=?", (bid,))
        c.execute("UPDATE batches SET status='submitted', pp_id=?, decided_by=? WHERE id=?", (pp_id, by, bid))
    return "submitted"


def reject(bid):
    with S.tx() as c:
        c.execute("UPDATE batches SET status='rejected', decided_by='human' WHERE id=? AND status='pending_approval'",
                  (bid,))


def refresh_open():
    """Poll PayPal for payout results (works without webhooks); webhooks just call this sooner."""
    for b in S.q("SELECT id,pp_id FROM batches WHERE status='submitted'"):
        try:
            data = pp().get_payout(b["pp_id"])
        except PayPalError:
            continue
        with S.tx() as c:
            for it in data.get("items", []):
                iid = it.get("payout_item", {}).get("sender_item_id", "")
                bid, _, mid = iid.rpartition(":")
                st = it.get("transaction_status", "")
                row = c.execute("SELECT * FROM items WHERE batch_id=? AND member_id=?", (bid, mid)).fetchone()
                if not row or row["status"] in ("paid", "failed"):
                    continue
                if st == "SUCCESS":
                    new = "paid"
                elif st in ("FAILED", "DENIED", "BLOCKED", "RETURNED", "REFUNDED", "REVERSED"):
                    new = "failed"  # give the money back to the member's accrual
                    c.execute("UPDATE members SET accrued=accrued+? WHERE id=?", (row["amount"], mid))
                elif st == "UNCLAIMED":
                    new = "unclaimed"
                else:
                    continue
                c.execute("UPDATE items SET status=? WHERE batch_id=? AND member_id=?", (new, bid, mid))
            left = c.execute("SELECT COUNT(*) n FROM items WHERE batch_id=? AND status IN ('sent','unclaimed')",
                             (b["id"],)).fetchone()["n"]
            if left == 0:
                c.execute("UPDATE batches SET status='settled' WHERE id=?", (b["id"],))
