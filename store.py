"""Config + SQLite store. All money is integer minor units (cents/pence)."""
import json, os, sqlite3, threading
from contextlib import contextmanager
from decimal import Decimal

REGION = os.environ.get("REGION", "US")
# PLACEHOLDER tariffs per kWh. Replace with real tariff data for your demo region.
REGIONS = {
    "US": {"currency": "USD", "buy": Decimal("0.28"), "sell": Decimal("0.08")},
    "UK": {"currency": "GBP", "buy": Decimal("0.27"), "sell": Decimal("0.15")},
    "EU": {"currency": "EUR", "buy": Decimal("0.30"), "sell": Decimal("0.08")},
}
R = REGIONS[REGION]
CURRENCY = R["currency"]


def _minor(key, default):
    return int(Decimal(os.environ.get(key, default)) * 100)


CFG = {
    "db": os.environ.get("DB_PATH", f"solarpool_{REGION}.db"),
    "ledger": os.environ.get("LEDGER_PATH", "blockchain_ledger.json"),
    "members": os.environ.get("MEMBERS_FILE", "members.json"),
    "verify": os.environ.get("LEDGER_VERIFY", "off"),      # off | links | full
    "min_payout": _minor("MIN_PAYOUT", "1.00"),             # batching threshold
    "cap_payout": _minor("CAP_PAYOUT", "50.00"),            # per-recipient hard flag
    "cap_batch": _minor("CAP_BATCH", "200.00"),
    "first_limit": _minor("FIRST_PAYOUT_LIMIT", "20.00"),
    "auto_limit": _minor("AUTO_APPROVE_LIMIT", "25.00"),    # batches above this need a human
    "interval": int(os.environ.get("SETTLE_INTERVAL_S", "60")),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS members(id TEXT PRIMARY KEY, email TEXT, role TEXT,
  balance INTEGER DEFAULT 0, accrued INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY, member_id TEXT, purpose TEXT,
  amount INTEGER, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS trades(key TEXT PRIMARY KEY, height INTEGER, seller TEXT, buyer TEXT,
  volume REAL, amount INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, pp_id TEXT, total INTEGER, status TEXT,
  risk TEXT, explanation TEXT, created REAL, decided_by TEXT);
CREATE TABLE IF NOT EXISTS items(batch_id TEXT, member_id TEXT, email TEXT, amount INTEGER,
  status TEXT, PRIMARY KEY(batch_id, member_id));
"""

_lock = threading.RLock()
_conn = None


def db():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(CFG["db"], check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        if os.path.exists(CFG["members"]):
            for mid, m in json.load(open(CFG["members"])).items():
                _conn.execute("INSERT INTO members(id,email,role) VALUES(?,?,?) ON CONFLICT(id) "
                              "DO UPDATE SET email=excluded.email, role=excluded.role",
                              (mid, m["email"], m.get("role", "")))
            _conn.commit()
    return _conn


@contextmanager
def tx():
    c = db()
    with _lock:
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in db().execute(sql, args).fetchall()]


def scalar(sql, args=()):
    rows = q(sql, args)
    return list(rows[0].values())[0] if rows else None


def money(minor):
    return f"{minor / 100:.2f}"
