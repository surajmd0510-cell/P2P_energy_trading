import os
import json
import time
import random
import datetime
import hashlib
import numpy as np
import requests
import paho.mqtt.client as mqtt

# ─────────────────────────────────────────────────────────────────────────────
# BROKER CONFIGURATION
# Python backend: raw TCP 1883. Browser dashboard: WebSocket 9001.
# /etc/mosquitto/mosquitto.conf needs:
#   listener 1883 / allow_anonymous true / listener 9001 / protocol websockets / allow_anonymous true
# ─────────────────────────────────────────────────────────────────────────────
BROKER = os.environ.get("MQTT_BROKER", "10.117.4.245")   # CHANGED: env override, same default
TICK = 3

# ─── TESTING OVERRIDE CONFIGURATION ───
# Set to an integer (0-23) to freeze the market clock at one hour.
TEST_OVERRIDE_HOUR = None

# ─── NEW: optional simulated clock (default off, so paper behaviour is unchanged) ───
# SIM_CLOCK=1 advances one market hour every TICKS_PER_HOUR ticks: a full 24 h day in ~12 min at 3 s ticks.
SIM_CLOCK      = os.environ.get("SIM_CLOCK", "0") == "1"
TICKS_PER_HOUR = int(os.environ.get("TICKS_PER_HOUR", "10"))
SIM_START_HOUR = int(os.environ.get("SIM_START_HOUR", "0"))
tick_counter   = 0
CURRENT_HOUR   = 0

# ─── NEW: engine-side balance gate ───
# 1 = original behaviour (a buyer whose engine balance is too low cannot trade).
# 0 = PayPal mode: solvency is enforced by the PayPal-funded wallet in the settlement service instead.
ENGINE_BALANCE_CHECK = os.environ.get("ENGINE_BALANCE_CHECK", "1") == "1"

# ---------------- FIXED CORES & HYPERPARAMETERS ---------------- #
FLOOR_PRICE  = 7.0
MAX_PRICE    = 13.0
MIN_BID      = 6.5
MAX_BID      = 10.95

# Bangalore Virtual Grid Tariff Structs (BESCOM)
GRID_BUY_PRICE  = 9.20
GRID_SELL_PRICE = 4.50

# RL Hyperparameters
ALPHA_RL = 0.2
GAMMA_RL = 0.9
EPSILON  = 0.15

# Market Structure Constants
K_FACTOR        = 0.25
GAMMA_SCARCITY  = 0.20   # legacy, unused
GAMMA_INV_SCARCITY      = 1.5
BERTRAND_UNDERCUT_DELTA = 0.10
PRICE_HISTORY_LEN       = 6
REPUTATION_DECAY        = 0.95

# ---------------- GRID CONFIGURATION MATRIX ---------------- #
houses = {
    "house1": {"type": "PROSUMER", "energy": 0, "ask": 7.5, "bid": 7.5, "battery": 5, "capacity": 10, "balance": 1000.0},
    "house2": {"type": "PROSUMER", "energy": 0, "ask": 7.5, "bid": 7.5, "battery": 5, "capacity": 10, "balance": 1000.0},
    "house3": {"type": "CONSUMER", "energy": 0, "ask": 7.5, "bid": 7.5, "battery": 5, "capacity": 10, "balance": 1000.0},
    "house4": {"type": "CONSUMER", "energy": 0, "ask": 7.5, "bid": 7.5, "battery": 5, "capacity": 10, "balance": 1000.0}
}

q_tables = {h_id: np.zeros((2 * 4, 3)) for h_id in houses.keys()}
hourly_trade_counts = {h_id: [0] * 24 for h_id in houses.keys()}
price_history = {h_id: [] for h_id in houses.keys()}
reputation = {h_id: 1.0 for h_id in houses.keys()}
reputation["GOVT_GRID"] = 1.0
peer_asks = {h_id: 7.5 for h_id in houses.keys()}

# ---------------- NEW: PERSISTENT LEARNING STATE (the paper says Q-tables are stored as JSON) ---------------- #
STATE_FILE = "q_tables.json"

def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"q_tables": {k: v.tolist() for k, v in q_tables.items()}, "reputation": reputation}, f)
    os.replace(tmp, STATE_FILE)

def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        for k, v in s.get("q_tables", {}).items():
            if k in q_tables:
                q_tables[k] = np.array(v, dtype=float)
        reputation.update(s.get("reputation", {}))
        print("🧠 Restored Q-tables and reputation from", STATE_FILE)
    except Exception as e:
        print("⚠️ Could not restore saved state:", e)

# ---------------- BULLETPROOF FILESYSTEM PARSING SCHEME ---------------- #
LEDGER_FILE = "blockchain_ledger.json"
blockchain_ledger = []

def write_ledger():
    """NEW: atomic write, so the settlement service never reads a half-written ledger."""
    tmp = LEDGER_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(blockchain_ledger, f, indent=4)
    os.replace(tmp, LEDGER_FILE)

if os.path.exists(LEDGER_FILE):
    try:
        with open(LEDGER_FILE, "r") as f:
            temp_ledger = json.load(f)
            if len(temp_ledger) == 0 or (isinstance(temp_ledger, list) and "current_block_hash" in temp_ledger[-1]):
                blockchain_ledger = temp_ledger
                print(f"📖 Loaded {len(blockchain_ledger)} historical secure blocks from ledger filesystem.")
                print("📊 Rebuilding chart metrics from secure ledger history...")
                for block in blockchain_ledger:
                    if block.get("status") == "SUCCESS":
                        try:
                            dt_obj     = datetime.datetime.strptime(block["timestamp"], "%Y-%m-%d %H:%M:%S.%f")
                            block_hour = block.get("sim_hour", dt_obj.hour)   # CHANGED: honours the simulated clock
                            s_id = block["seller_id"]
                            b_id = block["buyer_id"]
                            if s_id in hourly_trade_counts: hourly_trade_counts[s_id][block_hour] += 1
                            if b_id in hourly_trade_counts: hourly_trade_counts[b_id][block_hour] += 1
                        except Exception:
                            continue
            else:
                blockchain_ledger = []
    except Exception:
        blockchain_ledger = []

# ---------------- NASA POWER API WEATHER INTEGRATION ---------------- #
def get_nasa_solar_generation(current_hour):
    lat, lon = 12.9716, 77.5946
    url = "https://power.larc.nasa.gov/api/temporal/daily/point"
    params = {
        "parameters": "ALLSKY_SFC_SW_DWN,T2M",
        "community": "RE",
        "longitude": lon,
        "latitude": lat,
        "start": "20260501",
        "end": "20260525",
        "format": "JSON"
    }
    try:
        response = requests.get(url, params=params, timeout=3).json()
        irradiance_dict = response["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"]
        dates           = sorted(list(irradiance_dict.keys()))
        nasa_irradiance = irradiance_dict[dates[-1]]

        if current_hour < 6 or current_hour >= 18:
            time_multiplier = 0.0
        elif 11 <= current_hour <= 15:
            time_multiplier = 1.0
        else:
            time_multiplier = 0.5

        scaled_solar_generation = int((nasa_irradiance / 6.5) * 8 * time_multiplier)
        return max(0, min(8, scaled_solar_generation))
    except Exception:
        if current_hour >= 18 or current_hour < 6:
            return 0
        return 4

# ---------------- REALISTIC TIME-OF-DAY APPLIANCE SCHEDULER ENGINE ---------------- #
def calculate_appliance_consumption(house_id, current_hour):
    load_kw = 0.0
    is_lighting_peak = (18 <= current_hour <= 23)
    is_night_dim     = (0 <= current_hour <= 5)
    is_tv_peak       = (7 <= current_hour <= 9) or (17 <= current_hour <= 23)
    is_hot_afternoon = (12 <= current_hour <= 16)
    is_sleeping_time = (22 <= current_hour) or (current_hour <= 6)

    if house_id == "house1":
        load_kw += 0.25
        if is_lighting_peak:  load_kw += (10 * 0.012)
        elif is_night_dim:    load_kw += (1 * 0.012)
        else:                 load_kw += (2 * 0.012)
        if is_sleeping_time:  load_kw += (3 * 0.075)
        else:                 load_kw += (1 * 0.075)
        if is_tv_peak:        load_kw += (2 * 0.10)
        if is_hot_afternoon or is_sleeping_time:
            load_kw += 1.50

    elif house_id == "house2":
        load_kw += 0.25
        if is_lighting_peak:  load_kw += (5 * 0.012)
        elif is_night_dim:    load_kw += (1 * 0.012)
        if is_sleeping_time:  load_kw += (2 * 0.075)
        else:                 load_kw += (1 * 0.075)
        if is_tv_peak:        load_kw += (1 * 0.10)

    elif house_id == "house3":
        load_kw += 0.25
        if is_lighting_peak:  load_kw += (12 * 0.012)
        elif is_night_dim:    load_kw += (2 * 0.012)
        else:                 load_kw += (3 * 0.012)
        if is_sleeping_time:  load_kw += (3 * 0.075)
        else:                 load_kw += (2 * 0.075)
        if is_tv_peak:        load_kw += (1 * 0.10)
        if is_hot_afternoon or is_sleeping_time:
            load_kw += 1.50

    elif house_id == "house4":
        load_kw += 0.25
        if is_lighting_peak:  load_kw += (5 * 0.012)
        if is_sleeping_time:  load_kw += (1 * 0.075)
        if is_tv_peak:        load_kw += (1 * 0.10)

    return max(1, int(round(load_kw)))

# ---------------- STATE AND POLICY ENGINES ---------------- #
def get_state_index(net_market_energy, soc):
    scarcity = 1 if net_market_energy < 0 else 0
    if soc < 0.20:   soc_zone = 0
    elif soc < 0.40: soc_zone = 1
    elif soc < 0.80: soc_zone = 2
    else:            soc_zone = 3
    return (scarcity * 4) + soc_zone

def get_rl_action(house_id, state_idx):
    if random.random() < EPSILON:
        return random.randint(0, 2)
    return int(np.argmax(q_tables[house_id][state_idx]))

def inverse_scarcity_price(p_base, total_supply, total_demand, gamma=GAMMA_INV_SCARCITY, apply_floor=False):
    safe_demand = max(total_demand, 0.01)
    safe_supply = max(total_supply, 0.01)
    ratio = safe_supply / safe_demand
    raw   = p_base * (ratio ** (-gamma))
    if apply_floor:
        return float(np.clip(raw, FLOOR_PRICE, MAX_PRICE))
    return float(np.clip(raw, GRID_SELL_PRICE, MAX_PRICE))

def nash_bargaining_price(seller, s_name, buyer, b_name):
    soc_s = seller["battery"] / seller["capacity"]
    soc_b = buyer["battery"]  / buyer["capacity"]

    rep_s = reputation.get(s_name, 1.0)
    rep_b = reputation.get(b_name, 1.0)

    omega_s = max(0.05, soc_s * rep_s)
    omega_b = max(0.05, (1.0 - soc_b) * rep_b)

    d_b = GRID_BUY_PRICE
    d_s = GRID_SELL_PRICE

    p_star = (omega_b * d_s + omega_s * d_b) / (omega_b + omega_s)
    p_star = float(np.clip(p_star, GRID_SELL_PRICE, GRID_BUY_PRICE))

    print(f"   🔬 [NBS] SoC_s={soc_s:.2f} SoC_b={soc_b:.2f} | "
          f"ω_s={omega_s:.3f} ω_b={omega_b:.3f} → P*=₹{p_star:.4f}")
    return p_star

def bertrand_ask(house_id, soc, scarcity_price_signal, net_market_energy):
    soc_zone  = get_state_index(net_market_energy, soc)
    rl_action = get_rl_action(house_id, soc_zone)

    urgency_weight   = 0.40
    urgency_discount = soc * (GRID_BUY_PRICE - GRID_SELL_PRICE) * urgency_weight
    soc_ask_target   = GRID_BUY_PRICE - urgency_discount

    rival_asks = [peer_asks[rid] for rid, h in houses.items()
                  if rid != house_id and h["type"] == "PROSUMER"]
    if rival_asks:
        bertrand_base = min(rival_asks) - BERTRAND_UNDERCUT_DELTA
    else:
        bertrand_base = soc_ask_target

    bertrand_base = float(np.clip(bertrand_base, GRID_SELL_PRICE + 0.10, MAX_PRICE))

    rl_nudge = {0: -BERTRAND_UNDERCUT_DELTA / 2, 1: 0.0, 2: +BERTRAND_UNDERCUT_DELTA / 2}[rl_action]

    blended = (0.40 * soc_ask_target
             + 0.35 * (bertrand_base + rl_nudge)
             + 0.25 * scarcity_price_signal)

    return float(np.clip(round(blended, 2), GRID_SELL_PRICE + 0.10, MAX_PRICE))

def rl_predicted_price_action(house_id, current_price):
    history = price_history[house_id]
    history.append(current_price)
    if len(history) > PRICE_HISTORY_LEN:
        history.pop(0)

    if len(history) < 3:
        return 1

    x = np.arange(len(history), dtype=float)
    y = np.array(history, dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    predicted_next = intercept + slope * len(history)

    delta = predicted_next - current_price
    if delta > 0.15:    return 2
    elif delta < -0.15: return 0
    return 1

def update_reputation(agent_id, delivery_success):
    outcome = 1.0 if delivery_success else 0.0
    reputation[agent_id] = (REPUTATION_DECAY * reputation.get(agent_id, 1.0)
                            + (1.0 - REPUTATION_DECAY) * outcome)
    reputation[agent_id] = float(np.clip(reputation[agent_id], 0.01, 1.0))

# ---------------- CRYPTOGRAPHIC SMART CONTRACT ENGINE ---------------- #
def validate_and_log_smart_contract(s_name, seller, b_name, buyer, amount, bid_price, ask_price, clearing_price):
    # FIX: grid (P2G) trades are always honoured. Previously the buyer balance was checked AFTER it had
    # already been debited, so grid imports silently failed to reach the ledger once a balance ran low.
    is_utility = "GOVT_GRID" in (s_name, b_name)
    if not is_utility:
        if seller["battery"] <= 0 and seller["energy"] <= 0:
            return False
        if ENGINE_BALANCE_CHECK and buyer["balance"] < (amount * clearing_price):
            return False

    delivery_success = random.random() < 0.98
    previous_hash    = blockchain_ledger[-1]["current_block_hash"] if len(blockchain_ledger) > 0 else "00000000000000000000000000000000"
    timestamp_str    = str(datetime.datetime.now())

    # FIX: hash exactly the values stored in the block, so anyone can recompute and verify the chain.
    amount_r = round(amount, 4)
    price_r  = round(clearing_price, 2)
    data_payload = f"{previous_hash}{timestamp_str}{s_name}{b_name}{amount_r}{price_r}"
    current_hash = hashlib.sha256(data_payload.encode('utf-8')).hexdigest()

    block_receipt = {
        "block_height":             len(blockchain_ledger) + 1,
        "previous_block_hash":      previous_hash,
        "current_block_hash":       current_hash,
        "timestamp":                timestamp_str,
        "sim_hour":                 CURRENT_HOUR,
        "seller_id":                s_name,
        "buyer_id":                 b_name,
        "seller_initial_ask":       round(ask_price, 2),
        "buyer_initial_bid":        round(bid_price, 2),
        "clearing_price_per_kwh":   price_r,
        "energy_quantity_kw":       amount_r,
        "total_cost_inr":           round(amount * clearing_price, 2),
        "seller_soc_snapshot":      round(seller["battery"] / seller["capacity"], 2),
        "seller_reputation":        round(reputation.get(s_name, 1.0), 4),
        "buyer_reputation":         round(reputation.get(b_name, 1.0), 4),
        "status":                   "SUCCESS" if delivery_success else "REVERTED_DELIVERY_FAILURE"
    }

    blockchain_ledger.append(block_receipt)
    write_ledger()

    update_reputation(s_name, delivery_success)
    update_reputation(b_name, delivery_success)

    print(f"🔒 [BLOCKCHAIN SECURED] Transaction sealed at Block #{block_receipt['block_height']}")
    print(f"🔗 SHA-256 Signature: {current_hash[:20]}...")
    return delivery_success

def update_market_pricing_policies(net_market_energy, total_demand, total_supply):
    seller_scarcity_signal = inverse_scarcity_price(
        p_base=FLOOR_PRICE, total_supply=total_supply, total_demand=total_demand,
        gamma=GAMMA_INV_SCARCITY, apply_floor=False)

    display_scarcity_signal = inverse_scarcity_price(
        p_base=FLOOR_PRICE, total_supply=total_supply, total_demand=total_demand,
        gamma=GAMMA_INV_SCARCITY, apply_floor=True)

    print(f"   📈 [SCARCITY SIGNAL] Supply={total_supply:.1f} Demand={total_demand:.1f} "
          f"→ P_seller=₹{seller_scarcity_signal:.4f} | P_display=₹{display_scarcity_signal:.4f} (γ={GAMMA_INV_SCARCITY})")

    for name, h in houses.items():
        soc       = h["battery"] / h["capacity"]
        state_idx = get_state_index(net_market_energy, soc)

        if h["type"] == "PROSUMER":
            trend_action   = rl_predicted_price_action(name, h["ask"])
            rl_base_action = get_rl_action(name, state_idx)
            action = trend_action if random.random() < 0.70 else rl_base_action

            new_ask = bertrand_ask(name, soc, seller_scarcity_signal, net_market_energy)

            if action == 0: new_ask -= 0.25
            elif action == 2: new_ask += 0.25

            h["ask"] = float(np.clip(round(new_ask, 2), GRID_SELL_PRICE + 0.10, MAX_PRICE))
            peer_asks[name] = h["ask"]

        else:
            soc_b       = soc
            rep_b       = reputation.get(name, 1.0)
            omega_b     = max(0.05, (1.0 - soc_b) * rep_b)
            nbs_bid_ref = GRID_SELL_PRICE + omega_b * (GRID_BUY_PRICE - GRID_SELL_PRICE)

            trend_action = rl_predicted_price_action(name, h["bid"])
            rl_action    = get_rl_action(name, state_idx)
            action       = trend_action if random.random() < 0.70 else rl_action

            if action == 0: h["bid"] -= 0.25
            elif action == 2: h["bid"] += 0.25

            eq_bid = 0.60 * nbs_bid_ref + 0.40 * display_scarcity_signal
            h["bid"] = float(np.clip(round(0.70 * h["bid"] + 0.30 * eq_bid, 2), MIN_BID, MAX_BID))

# ---------------- MQTT BROKER UTILITIES ---------------- #
def on_connect(client, userdata, flags, rc):
    print("Central Broker Network Interface Connected.")

def make_client():
    """NEW: works with paho-mqtt 1.x and 2.x."""
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except AttributeError:
        c = mqtt.Client()
    c.on_connect = on_connect
    return c

def get_current_hour():
    if TEST_OVERRIDE_HOUR is not None:
        return TEST_OVERRIDE_HOUR
    if SIM_CLOCK:
        return (SIM_START_HOUR + tick_counter // TICKS_PER_HOUR) % 24
    return datetime.datetime.now().hour

# ---------------- INTERNAL STORAGE MANAGEMENT ---------------- #
def handle_battery(h):
    if h["energy"] > 0:
        soc = h["battery"] / h["capacity"]
        if soc >= 0.80: return
        space  = h["capacity"] - h["battery"]
        charge = min(space, h["energy"])
        h["battery"] += charge
        h["energy"]  -= charge
    elif h["energy"] < 0:
        soc = h["battery"] / h["capacity"]
        if soc <= 0.20: return
        need      = abs(h["energy"])
        discharge = min(h["battery"], need)
        h["battery"] -= discharge
        h["energy"]  += discharge

def match_and_trade(client):
    global CURRENT_HOUR
    current_hour = get_current_hour()
    CURRENT_HOUR = current_hour

    nasa_generation = get_nasa_solar_generation(current_hour)
    print(f"\n⚡ [CLOCK TRACKER] Executing Matrix at Hour: {current_hour:02d}:00")

    pre_storage_demand = 0
    pre_storage_supply = 0

    sdr_report_payload = {"hour": current_hour, "sdr_values": {}}

    for name, h in houses.items():
        appliance_load     = calculate_appliance_consumption(name, current_hour)
        actual_generation  = nasa_generation if h["type"] == "PROSUMER" else 0
        h["energy"]        = actual_generation - appliance_load

        if h["energy"] > 0:  pre_storage_supply += h["energy"]
        else:                 pre_storage_demand += abs(h["energy"])

        calculated_sdr = (
            round(float(actual_generation) / float(appliance_load), 2)
            if appliance_load > 0 else float(actual_generation)
        )
        sdr_report_payload["sdr_values"][name] = calculated_sdr

    client.publish("grid/sdr_report", json.dumps(sdr_report_payload), retain=True)

    for name, h in houses.items():
        appliance_load    = calculate_appliance_consumption(name, current_hour)
        actual_generation = nasa_generation if h["type"] == "PROSUMER" else 0
        client.publish(f"{name}/generation",  str(actual_generation))
        client.publish(f"{name}/consumption", str(appliance_load))

    net_market_energy = pre_storage_supply - pre_storage_demand

    update_market_pricing_policies(net_market_energy, pre_storage_demand, pre_storage_supply)

    buyers  = []
    sellers = []
    for name, h in houses.items():
        handle_battery(h)
        if h["energy"] > 0:   sellers.append({"name": name, "data": h})
        elif h["energy"] < 0: buyers.append({"name": name, "data": h})

    print("\n======================= SYSTEM MICROGRID STATE HORIZON =======================")
    for n, h in houses.items():
        price_metric = f"Ask: ₹{h['ask']}" if h["type"] == "PROSUMER" else f"Bid: ₹{h['bid']}"
        rep_str      = f"Rep: {reputation.get(n, 1.0):.3f}"
        print(f"{n.upper()} ({h['type']}) -> Net Target: {h['energy']:2d} kW | "
              f"Storage: {h['battery']}/{h['capacity']} | {price_metric} | "
              f"Balance: ₹{round(h['balance'],2)} | {rep_str}")

    sellers.sort(key=lambda x: x["data"]["ask"])
    buyers.sort(key=lambda x: -x["data"]["bid"])

    print("\n📊 --- P2P DOUBLE AUCTION ORDER BOOKS COMPILING ---")
    print("Local Buyers:",  [(b["name"], f"₹{b['data']['bid']}") for b in buyers  if abs(b["data"]["energy"]) > 0])
    print("Local Sellers:", [(s["name"], f"₹{s['data']['ask']}") for s in sellers if s["data"]["energy"] > 0])

    # ─── PHASE 1: PEER-TO-PEER LOCAL TRADING (NBS Clearing) ───
    i, j = 0, 0
    while i < len(buyers) and j < len(sellers):
        buyer_node  = buyers[i]
        seller_node = sellers[j]

        b_name, buyer  = buyer_node["name"],  buyer_node["data"]
        s_name, seller = seller_node["name"], seller_node["data"]

        if abs(buyer["energy"])  == 0: i += 1; continue
        if     seller["energy"] == 0:  j += 1; continue

        nbs_preview = nash_bargaining_price(seller, s_name, buyer, b_name)
        if nbs_preview < GRID_SELL_PRICE or nbs_preview > GRID_BUY_PRICE:
            print(f"\n⚠️ [NBS INFEASIBLE] P*=₹{nbs_preview:.2f} outside surplus band. Skipping pair.")
            i += 1
            continue

        if buyer["bid"] < GRID_SELL_PRICE and nbs_preview > buyer["bid"] * 1.5:
            print(f"\n⚠️ [SPREAD GAP] Bid ₹{buyer['bid']} too far below NBS ₹{nbs_preview:.2f}. → P2G.")
            break

        amount = min(seller["energy"], abs(buyer["energy"]))

        price = nbs_preview
        cost  = amount * price

        is_block_verified = validate_and_log_smart_contract(
            s_name, seller, b_name, buyer, amount, buyer["bid"], seller["ask"], price)

        soc_s  = seller["battery"] / seller["capacity"]
        soc_b  = buyer["battery"]  / buyer["capacity"]
        st_s   = get_state_index(net_market_energy, soc_s)
        st_b   = get_state_index(net_market_energy, soc_b)
        act_s  = int(np.argmax(q_tables[s_name][st_s]))
        act_b  = int(np.argmax(q_tables[b_name][st_b]))

        if not is_block_verified:
            q_tables[s_name][st_s][act_s] += ALPHA_RL * (-3.0 - q_tables[s_name][st_s][act_s])
            j += 1
            continue

        hourly_trade_counts[s_name][current_hour] += 1
        hourly_trade_counts[b_name][current_hour]  += 1

        seller["energy"] -= amount
        buyer["energy"]  += amount
        buyer["balance"] -= cost
        seller["balance"] += cost

        handle_battery(seller)
        handle_battery(buyer)

        print(f"⚡ DOUBLE AUCTION TRADE CLEARED | NBS PRICE: ₹{price:.4f}/kWh")
        print(f"   Match: {s_name} → {b_name} | Volume: {amount} Units | Total: ₹{round(cost,2)}")

        timestamp_str = str(datetime.datetime.now())
        trade_event_payload = {
            "block_id":     len(blockchain_ledger),
            "time":         timestamp_str,
            "seller":       s_name,
            "buyer":        b_name,
            "volume":       round(amount, 2),
            "price":        round(price, 2),
            "total_cost":   round(cost, 2),
            "clearing_mechanism": "NBS",
            "seller_reputation":  round(reputation.get(s_name, 1.0), 4),
            "buyer_reputation":   round(reputation.get(b_name, 1.0), 4)
        }
        client.publish(f"{s_name}/trade_event", json.dumps(trade_event_payload))

        seller_surplus = (price - GRID_SELL_PRICE) * amount
        buyer_surplus  = (GRID_BUY_PRICE - price) * amount

        reward_s = seller_surplus - (2.0 * (1.0 - soc_s))
        reward_b = buyer_surplus

        q_tables[s_name][st_s][act_s] += ALPHA_RL * (reward_s + GAMMA_RL * np.max(q_tables[s_name][st_s]) - q_tables[s_name][st_s][act_s])
        q_tables[b_name][st_b][act_b] += ALPHA_RL * (reward_b + GAMMA_RL * np.max(q_tables[b_name][st_b]) - q_tables[b_name][st_b][act_b])

        client.publish(f"{s_name}/trade", "1", retain=True)
        client.publish(f"{b_name}/trade", "1", retain=True)
        time.sleep(0.05)
        client.publish(f"{s_name}/trade", "0", retain=True)
        client.publish(f"{b_name}/trade", "0", retain=True)

        client.publish(f"{s_name}/grid_status", "BALANCED", retain=True)
        client.publish(f"{b_name}/grid_status", "BALANCED", retain=True)

        client.publish(f"{s_name}/reputation", str(round(reputation.get(s_name, 1.0), 4)), retain=True)
        client.publish(f"{b_name}/reputation", str(round(reputation.get(b_name, 1.0), 4)), retain=True)

        if seller["energy"] == 0: j += 1
        if buyer["energy"]  == 0: i += 1

    # ─── PHASE 2: AUTOMATIC PEER-TO-GRID (P2G) FALLBACK ENGINE ───
    print("\n🌐 --- PEER-TO-GRID (P2G) FALLBACK BROKERAGE ACTIVE ---")
    timestamp_str = str(datetime.datetime.now())

    for buyer_node in buyers:
        b_name, buyer = buyer_node["name"], buyer_node["data"]
        unmet_demand  = abs(buyer["energy"])

        if unmet_demand > 0:
            cost = unmet_demand * GRID_BUY_PRICE
            buyer["balance"] -= cost
            buyer["energy"]   = 0
            handle_battery(buyer)

            print(f"🔌 [GRID BUY] {b_name} imported {unmet_demand} kW @ ₹{GRID_BUY_PRICE}/kWh. Cost: ₹{round(cost,2)}")
            validate_and_log_smart_contract(
                "GOVT_GRID", {"battery": 999, "energy": 999, "capacity": 999},
                b_name, buyer, unmet_demand, GRID_BUY_PRICE, GRID_BUY_PRICE, GRID_BUY_PRICE)
            client.publish(f"{b_name}/grid_status", "BUY", retain=True)

            grid_buy_payload = {
                "block_id":   len(blockchain_ledger),
                "time":       timestamp_str,
                "seller":     "GOVT_GRID",
                "buyer":      b_name,
                "volume":     round(unmet_demand, 2),
                "price":      GRID_BUY_PRICE,
                "total_cost": round(cost, 2),
                "clearing_mechanism": "P2G_FALLBACK"
            }
            client.publish(f"{b_name}/trade_event", json.dumps(grid_buy_payload))

    for seller_node in sellers:
        s_name, seller = seller_node["name"], seller_node["data"]
        excess_supply  = seller["energy"]

        if excess_supply > 0:
            revenue = excess_supply * GRID_SELL_PRICE
            seller["balance"] += revenue
            seller["energy"]   = 0
            handle_battery(seller)

            print(f"📡 [GRID SELL] {s_name} exported {excess_supply} kW @ ₹{GRID_SELL_PRICE}/kWh. Revenue: ₹{round(revenue,2)}")
            validate_and_log_smart_contract(
                s_name, seller, "GOVT_GRID", {"balance": 999999},
                excess_supply, GRID_SELL_PRICE, GRID_SELL_PRICE, GRID_SELL_PRICE)
            client.publish(f"{s_name}/grid_status", "SELL", retain=True)

            grid_sell_payload = {
                "block_id":   len(blockchain_ledger),
                "time":       timestamp_str,
                "seller":     s_name,
                "buyer":      "GOVT_GRID",
                "volume":     round(excess_supply, 2),
                "price":      GRID_SELL_PRICE,
                "total_cost": round(revenue, 2),
                "clearing_mechanism": "P2G_FALLBACK"
            }
            client.publish(f"{s_name}/trade_event", json.dumps(grid_sell_payload))

    for name, h in houses.items():
        client.publish(f"{name}/update",     str(h["energy"]))
        client.publish(f"{name}/battery",    str(h["battery"]))
        client.publish(f"{name}/balance",    str(h["balance"]))
        client.publish(f"{name}/reputation", str(round(reputation.get(name, 1.0), 4)), retain=True)
        if h["type"] == "PROSUMER":
            client.publish(f"{name}/ask", str(round(h["ask"], 2)), retain=True)
        else:
            client.publish(f"{name}/bid", str(round(h["bid"], 2)), retain=True)

    client.publish("grid/hourly_trades", json.dumps(hourly_trade_counts), retain=True)

    safe_demand     = max(pre_storage_demand, 0.01)
    safe_supply     = max(pre_storage_supply, 0.01)
    live_scarcity_seller  = inverse_scarcity_price(FLOOR_PRICE, safe_supply, safe_demand, apply_floor=False)
    live_scarcity_display = inverse_scarcity_price(FLOOR_PRICE, safe_supply, safe_demand, apply_floor=True)
    scarcity_payload = {
        "hour":                   current_hour,
        "supply":                 round(pre_storage_supply, 2),
        "demand":                 round(pre_storage_demand, 2),
        "scarcity_price_seller":  round(live_scarcity_seller, 4),
        "scarcity_price_display": round(live_scarcity_display, 4),
        "gamma":                  GAMMA_INV_SCARCITY
    }
    client.publish("grid/scarcity_signal", json.dumps(scarcity_payload), retain=True)

# ---------------- Central Entry Core Execution ---------------- #
# CHANGED: guarded so the module can be imported and tested without starting the loop.
if __name__ == "__main__":
    load_state()
    client = make_client()
    while True:
        try:
            client.connect(BROKER, 1883)
            break
        except OSError as e:
            print(f"Broker {BROKER} unreachable ({e}); retrying in 5 s...")
            time.sleep(5)
    client.loop_start()

    try:
        while True:
            time.sleep(TICK)
            match_and_trade(client)
            tick_counter += 1
            if tick_counter % 20 == 0:
                save_state()
    except KeyboardInterrupt:
        save_state()
        print("Stopped. Learning state saved.")
