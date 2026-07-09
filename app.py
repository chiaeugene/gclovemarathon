# -*- coding: utf-8 -*-
"""GC Marathon Spin Wheel — Flask app.

Pulls participant spin counts live from the Master Google Sheet (or from
manual admin edits), tracks prize inventory (quantity-based, auto-removes
sold-out prizes), and logs every spin result so it can be exported / pushed
back to a "SPIN RESULTS" tab in the same spreadsheet.

Local JSON files are the fast runtime store. On every mutation the same
data is also backed up into a hidden "APP STATE" tab in the spreadsheet,
and on startup — if the local files don't exist (e.g. a fresh container on
a redeploy) — state is restored from that backup. This makes the app safe
to run on hosts with ephemeral disks (Render, Railway, etc.).
"""
import json, os, random, urllib.parse
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
import requests
from dotenv import load_dotenv

load_dotenv()  # loads .env for local dev; on Render, real env vars are set in the dashboard instead

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "state.json")
PRIZES_FILE = os.path.join(BASE, "prizes.json")
PRIZES_BASELINE_FILE = os.path.join(BASE, "prizes_baseline.json")
RESULTS_FILE = os.path.join(BASE, "results.json")
SPECIAL_FILE = os.path.join(BASE, "special.json")
RAFFLES_FILE = os.path.join(BASE, "raffles.json")

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
MASTER_SHEET_ID = os.environ["MASTER_SHEET_ID"]

app = Flask(__name__, static_folder="static", static_url_path="")

# ---------------------------------------------------------------- storage --
def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def default_prizes():
    return {
        "normal": [
            {"id": "n1", "name": "Ampoule Mask", "qty": 1},
            {"id": "n2", "name": "HUAT AH", "qty": 3},
            {"id": "n3", "name": "RM18 Cash", "qty": 3},
            {"id": "n4", "name": "RM8 Cash", "qty": 5},
            {"id": "n5", "name": "RM3.88 Cash", "qty": 8},
            {"id": "n6", "name": "RM38 Cash", "qty": 1},
        ],
        "special": [
            {"id": "s1", "name": "RM50 Voucher", "qty": 5},
            {"id": "s2", "name": "RM100 Voucher", "qty": 3},
            {"id": "s3", "name": "Premium Gift", "qty": 5},
        ],
    }

def get_prizes():
    return load_json(PRIZES_FILE, default_prizes())

def get_prizes_baseline():
    return load_json(PRIZES_BASELINE_FILE, get_prizes())

def get_state():
    return load_json(STATE_FILE, {"participants": [], "last_synced": None})

def get_results():
    return load_json(RESULTS_FILE, [])

def default_special():
    return {
        "gifts": [
            "Dior Lipstick Case + Lipstick",
            "1 Box MAE Product Worth RM288 (Airblur / Perfector / Clarity / Serum / Hair Shampoo / iReason)",
            "RM68 Cash Ang Pao",
        ],
        "awarded": [],  # [{no, name, gift, ts}, ...] in draw order
    }

def get_special():
    data = load_json(SPECIAL_FILE, default_special())
    data.setdefault("gifts", default_special()["gifts"])
    data.setdefault("awarded", [])
    return data

def interleave_pool(pool, key_fn):
    """Round-robin the pool so one person's multiple tickets are spread
    around the wheel instead of sitting in adjacent wedges. Order is a pure
    function of the input, so it stays stable across repeated computations
    of the same underlying state (no flicker on redraw)."""
    groups = {}
    order = []
    for item in pool:
        k = key_fn(item)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(item)
    result = []
    while any(groups[k] for k in order):
        for k in order:
            if groups[k]:
                result.append(groups[k].pop(0))
    return result

def compute_special_pool(state=None, special=None):
    """One ticket per remaining special spin, per participant — excludes
    anyone who has already won a gift (their whole ticket set is removed)."""
    state = state if state is not None else get_state()
    special = special if special is not None else get_special()
    won_nos = {a["no"] for a in special["awarded"]}
    pool = []
    for p in state.get("participants", []):
        if p.get("special_total", 0) > 0 and p["no"] not in won_nos:
            pool.extend([{"no": p["no"], "name": p["name"]}] * p["special_total"])
    return interleave_pool(pool, lambda item: item["no"])

# ---------------------------------------------------- generic named raffles --
# Same raffle mechanic as the special wheel (one ticket per person, winner's
# whole ticket set removed after winning, gifts awarded in a fixed sequence),
# but with an independent roster — for one-off draws (e.g. a graduation lucky
# draw) whose entrants aren't necessarily in the main marathon roster at all.
def default_raffles():
    return {
        "graduation": {
            "label": "Graduation Reward",
            "gifts": ["Ampoule Mask", "Ampoule Mask", "Clarity Purple"],
            "roster": [
                {"name": "Chang Ee Wei", "tickets": 1},
                {"name": "Low Chai Ying", "tickets": 1},
                {"name": "Kerryn Lim", "tickets": 1},
                {"name": "Chia Wei Ping", "tickets": 1},
                {"name": "Chloe Chan", "tickets": 1},
                {"name": "Jasmine Abigail", "tickets": 1},
            ],
            "awarded": [],  # [{name, gift, ts}, ...] in draw order
        },
    }

def get_raffles():
    data = load_json(RAFFLES_FILE, default_raffles())
    for rid, defaults in default_raffles().items():
        data.setdefault(rid, defaults)
    for robj in data.values():
        robj.setdefault("gifts", [])
        robj.setdefault("roster", [])
        robj.setdefault("awarded", [])
    return data

def compute_raffle_pool(raffle):
    won_names = {a["name"] for a in raffle["awarded"]}
    pool = []
    for person in raffle["roster"]:
        if person["name"] not in won_names:
            pool.extend([{"name": person["name"]}] * max(1, int(person.get("tickets", 1))))
    return interleave_pool(pool, lambda item: item["name"])

# ------------------------------------------------------------ google auth --
def get_token():
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN, "grant_type": "refresh_token"})
    r.raise_for_status()
    return r.json()["access_token"]

# --------------------------------------------------- cloud backup (Sheet) --
# Keeps the app usable on hosts with ephemeral disks: every mutation is
# mirrored into a hidden "APP STATE" tab, and on cold start we try to
# restore from it before falling back to defaults.
APP_STATE_TAB = "APP STATE"

def cloud_backup():
    try:
        token = get_token()
        H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        HA = {"Authorization": f"Bearer {token}"}
        meta = requests.get(f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SHEET_ID}", headers=HA, timeout=10).json()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if APP_STATE_TAB not in titles:
            requests.post(f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SHEET_ID}:batchUpdate",
                headers=H, json={"requests": [{"addSheet": {"properties": {"title": APP_STATE_TAB}}}]}, timeout=10)
        payload = {
            "state": get_state(), "prizes": get_prizes(),
            "prizes_baseline": get_prizes_baseline(), "results": get_results(),
            "special": get_special(), "raffles": get_raffles(),
        }
        rng = urllib.parse.quote(f"{APP_STATE_TAB}!A1")
        resp = requests.put(
            f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SHEET_ID}/values/{rng}"
            f"?valueInputOption=RAW", headers=H,
            json={"values": [["blob", json.dumps(payload)]]}, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print("cloud_backup failed (non-fatal):", e)

def cloud_restore_if_empty():
    if os.path.exists(STATE_FILE):
        return  # local disk already has data, nothing to restore
    try:
        token = get_token()
        HA = {"Authorization": f"Bearer {token}"}
        rng = urllib.parse.quote(f"{APP_STATE_TAB}!A1:B1")
        resp = requests.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SHEET_ID}/values/{rng}",
            headers=HA, timeout=10)
        if resp.status_code != 200:
            return
        values = resp.json().get("values", [])
        if not values or len(values[0]) < 2:
            return
        payload = json.loads(values[0][1])
        save_json(STATE_FILE, payload.get("state", {"participants": [], "last_synced": None}))
        save_json(PRIZES_FILE, payload.get("prizes", default_prizes()))
        save_json(PRIZES_BASELINE_FILE, payload.get("prizes_baseline", default_prizes()))
        save_json(RESULTS_FILE, payload.get("results", []))
        save_json(SPECIAL_FILE, payload.get("special", default_special()))
        save_json(RAFFLES_FILE, payload.get("raffles", default_raffles()))
        print("Restored state from cloud backup.")
    except Exception as e:
        print("cloud_restore_if_empty failed (non-fatal):", e)

cloud_restore_if_empty()
if not os.path.exists(STATE_FILE):
    save_json(STATE_FILE, {"participants": [], "last_synced": None})
if not os.path.exists(PRIZES_FILE):
    save_json(PRIZES_FILE, default_prizes())
if not os.path.exists(PRIZES_BASELINE_FILE):
    save_json(PRIZES_BASELINE_FILE, get_prizes())
if not os.path.exists(RESULTS_FILE):
    save_json(RESULTS_FILE, [])
if not os.path.exists(SPECIAL_FILE):
    save_json(SPECIAL_FILE, default_special())
if not os.path.exists(RAFFLES_FILE):
    save_json(RAFFLES_FILE, default_raffles())

# ---------------------------------------------------------------- routes --
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/sync", methods=["POST"])
def sync():
    """Pull NO, NAME, ST5 SPECIAL SPINS (L), TOTAL SPINS (O) from Master.
    Preserves already-used spin counts for existing participants."""
    token = get_token()
    HA = {"Authorization": f"Bearer {token}"}
    rng = urllib.parse.quote("MASTER!A2:O49")
    resp = requests.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SHEET_ID}/values/{rng}"
        f"?valueRenderOption=UNFORMATTED_VALUE", headers=HA)
    resp.raise_for_status()
    rows = resp.json().get("values", [])

    state = get_state()
    existing = {p["no"]: p for p in state.get("participants", [])}
    new_list = []
    for row in rows:
        def c(i): return row[i] if i < len(row) else ""
        no = c(0)
        name = c(1)
        if not no or not name:
            continue
        try:
            special_total = int(float(c(11) or 0))
        except Exception:
            special_total = 0
        try:
            normal_total = int(float(c(14) or 0))
        except Exception:
            normal_total = 0

        prev = existing.get(no, {})
        normal_used = min(prev.get("normal_used", 0), normal_total)
        special_used = min(prev.get("special_used", 0), special_total)
        new_list.append({
            "no": no, "name": name,
            "normal_total": normal_total, "normal_used": normal_used,
            "special_total": special_total, "special_used": special_used,
        })
    state = {"participants": new_list, "last_synced": datetime.now().isoformat(timespec="seconds")}
    save_json(STATE_FILE, state)
    cloud_backup()
    return jsonify(state)

@app.route("/api/state", methods=["GET"])
def api_state():
    special = get_special()
    raffles = get_raffles()
    return jsonify({
        "state": get_state(),
        "prizes": get_prizes(),
        "results": get_results(),
        "special": special,
        "specialPool": compute_special_pool(special=special),
        "raffles": {
            rid: {**robj, "pool": compute_raffle_pool(robj)}
            for rid, robj in raffles.items()
        },
    })

@app.route("/api/prizes", methods=["POST"])
def set_prizes():
    """Saving the prize list also becomes the new reset baseline — this is
    the admin declaring 'this is the full stock available going forward'."""
    body = request.get_json()
    save_json(PRIZES_FILE, body)
    save_json(PRIZES_BASELINE_FILE, body)
    cloud_backup()
    return jsonify(body)

@app.route("/api/participants", methods=["POST"])
def set_participants():
    """Manual admin override of the participant roster (name + spin totals),
    independent of the Google Sheet. Used for last-minute changes. Existing
    used-spin counts are preserved (capped to the new totals)."""
    body = request.get_json()
    incoming = body.get("participants", [])
    state = get_state()
    existing = {p["no"]: p for p in state.get("participants", [])}

    new_list = []
    for row in incoming:
        no = row.get("no")
        name = (row.get("name") or "").strip()
        if not no or not name:
            continue
        try:
            normal_total = max(0, int(row.get("normal_total", 0)))
        except Exception:
            normal_total = 0
        try:
            special_total = max(0, int(row.get("special_total", 0)))
        except Exception:
            special_total = 0
        prev = existing.get(no, {})
        normal_used = min(prev.get("normal_used", 0), normal_total)
        special_used = min(prev.get("special_used", 0), special_total)
        new_list.append({
            "no": no, "name": name,
            "normal_total": normal_total, "normal_used": normal_used,
            "special_total": special_total, "special_used": special_used,
        })
    state = {"participants": new_list, "last_synced": state.get("last_synced")}
    save_json(STATE_FILE, state)
    cloud_backup()
    return jsonify(state)

@app.route("/api/spin", methods=["POST"])
def spin():
    """Normal wheel only — pick a random prize for the given participant.
    The special wheel is a separate raffle mechanic (see /api/special-draw)."""
    body = request.get_json()
    no = body.get("no")
    wheel = body.get("wheel")
    if wheel != "normal":
        return jsonify({"error": "bad wheel"}), 400

    state = get_state()
    participant = next((p for p in state["participants"] if p["no"] == no), None)
    if not participant:
        return jsonify({"error": "participant not found"}), 404

    total_key = f"{wheel}_total"
    used_key = f"{wheel}_used"
    if participant[used_key] >= participant[total_key]:
        return jsonify({"error": "no spins remaining"}), 400

    prizes = get_prizes()
    pool = prizes[wheel]
    weights = [max(0, p.get("qty", 0)) for p in pool]
    if not pool or sum(weights) <= 0:
        return jsonify({"error": "no prizes configured for this wheel"}), 400

    # qty is a relative weight here, not depleting stock — the normal wheel
    # never runs out or loses wedges, unlike the special/raffle wheels.
    chosen = random.choices(pool, weights=weights, k=1)[0]

    participant[used_key] += 1
    save_json(STATE_FILE, state)

    result = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "no": no, "name": participant["name"],
        "wheel": wheel, "prize_id": chosen["id"], "prize_name": chosen["name"],
    }
    results = get_results()
    results.append(result)
    save_json(RESULTS_FILE, results)
    cloud_backup()

    return jsonify({
        "result": result,
        "participant": participant,
        "wheel_pool": prizes[wheel],  # updated quantities, for wheel to redraw
    })

@app.route("/api/undo", methods=["POST"])
def undo():
    """Undo the most recent spin for a given participant (mis-click safety net)."""
    body = request.get_json()
    no = body.get("no")
    results = get_results()
    idx = None
    for i in range(len(results) - 1, -1, -1):
        if results[i]["no"] == no:
            idx = i
            break
    if idx is None:
        return jsonify({"error": "no spins to undo"}), 400
    r = results.pop(idx)
    save_json(RESULTS_FILE, results)

    # No prize quantity to restore — normal-wheel qty is a weight, not
    # depleting stock (see /api/spin), so nothing was decremented.
    state = get_state()
    for p in state["participants"]:
        if p["no"] == no:
            p[f"{r['wheel']}_used"] = max(0, p[f"{r['wheel']}_used"] - 1)
            break
    save_json(STATE_FILE, state)
    cloud_backup()
    return jsonify({"undone": r})

# ------------------------------------------------------ special raffle --
# The special wheel is a shared raffle, not per-person spins: the wheel is
# populated with one ticket per remaining special spin (so someone with 2
# special spins gets 2 tickets), each draw awards the NEXT gift in a fixed
# sequence to whoever's ticket comes up, and the winner's entire ticket set
# is then removed — so nobody can win a second special gift.
@app.route("/api/special-draw", methods=["POST"])
def special_draw():
    special = get_special()
    gifts = special["gifts"]
    awarded = special["awarded"]
    if len(awarded) >= len(gifts):
        return jsonify({"error": "All special gifts have already been awarded."}), 400

    state = get_state()
    pool = compute_special_pool(state, special)
    if not pool:
        return jsonify({"error": "No one left in the special draw."}), 400

    winner = random.choice(pool)
    gift = gifts[len(awarded)]
    award = {
        "no": winner["no"], "name": winner["name"], "gift": gift,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    awarded.append(award)
    save_json(SPECIAL_FILE, special)
    cloud_backup()

    return jsonify({
        "result": award,
        "special": special,
        "specialPool": compute_special_pool(state, special),
    })

@app.route("/api/special-undo", methods=["POST"])
def special_undo():
    special = get_special()
    if not special["awarded"]:
        return jsonify({"error": "No special draws to undo"}), 400
    undone = special["awarded"].pop()
    save_json(SPECIAL_FILE, special)
    cloud_backup()
    return jsonify({
        "undone": undone,
        "special": special,
        "specialPool": compute_special_pool(special=special),
    })

@app.route("/api/special-gifts", methods=["POST"])
def set_special_gifts():
    """Admin edits the ordered gift list. Changing the list does not touch
    who has already won — only future draws use the new list."""
    body = request.get_json()
    gifts = [g.strip() for g in body.get("gifts", []) if g.strip()]
    special = get_special()
    special["gifts"] = gifts
    save_json(SPECIAL_FILE, special)
    cloud_backup()
    return jsonify(special)

# ---------------------------------------------------- generic named raffles --
@app.route("/api/raffle-draw", methods=["POST"])
def raffle_draw():
    body = request.get_json()
    rid = body.get("raffle")
    raffles = get_raffles()
    robj = raffles.get(rid)
    if not robj:
        return jsonify({"error": "unknown raffle"}), 404
    if len(robj["awarded"]) >= len(robj["gifts"]):
        return jsonify({"error": "All gifts have already been awarded."}), 400

    pool = compute_raffle_pool(robj)
    if not pool:
        return jsonify({"error": "No one left in this draw."}), 400

    winner = random.choice(pool)
    gift = robj["gifts"][len(robj["awarded"])]
    award = {"name": winner["name"], "gift": gift, "ts": datetime.now().isoformat(timespec="seconds")}
    robj["awarded"].append(award)
    save_json(RAFFLES_FILE, raffles)
    cloud_backup()

    return jsonify({"result": award, "raffle": robj, "pool": compute_raffle_pool(robj)})

@app.route("/api/raffle-undo", methods=["POST"])
def raffle_undo():
    body = request.get_json()
    rid = body.get("raffle")
    raffles = get_raffles()
    robj = raffles.get(rid)
    if not robj:
        return jsonify({"error": "unknown raffle"}), 404
    if not robj["awarded"]:
        return jsonify({"error": "No draws to undo"}), 400
    undone = robj["awarded"].pop()
    save_json(RAFFLES_FILE, raffles)
    cloud_backup()
    return jsonify({"undone": undone, "raffle": robj, "pool": compute_raffle_pool(robj)})

@app.route("/api/raffle-roster", methods=["POST"])
def set_raffle_roster():
    """Admin edits a raffle's label, gift sequence, and roster (name + ticket
    count). Doesn't touch who's already won — only future draws are affected."""
    body = request.get_json()
    rid = body.get("raffle")
    if not rid:
        return jsonify({"error": "missing raffle id"}), 400
    raffles = get_raffles()
    robj = raffles.get(rid, {"label": rid, "gifts": [], "roster": [], "awarded": []})

    robj["label"] = (body.get("label") or robj["label"]).strip()
    robj["gifts"] = [g.strip() for g in body.get("gifts", []) if g.strip()]
    roster = []
    for row in body.get("roster", []):
        name = (row.get("name") or "").strip()
        if not name:
            continue
        try:
            tickets = max(1, int(row.get("tickets", 1)))
        except Exception:
            tickets = 1
        roster.append({"name": name, "tickets": tickets})
    robj["roster"] = roster

    raffles[rid] = robj
    save_json(RAFFLES_FILE, raffles)
    cloud_backup()
    return jsonify({**robj, "pool": compute_raffle_pool(robj)})

@app.route("/api/reset", methods=["POST"])
def reset_event():
    """Full reset for dry-runs: every participant gets their spins back,
    prize quantities are restored from the baseline (whatever was last
    saved in Manage Prizes), and the results log is cleared."""
    state = get_state()
    for p in state.get("participants", []):
        p["normal_used"] = 0
        p["special_used"] = 0
    save_json(STATE_FILE, state)

    baseline = get_prizes_baseline()
    save_json(PRIZES_FILE, json.loads(json.dumps(baseline)))

    save_json(RESULTS_FILE, [])

    special = get_special()
    special["awarded"] = []
    save_json(SPECIAL_FILE, special)

    raffles = get_raffles()
    for robj in raffles.values():
        robj["awarded"] = []
    save_json(RAFFLES_FILE, raffles)

    cloud_backup()
    return jsonify({
        "state": state, "prizes": get_prizes(), "results": [],
        "special": special, "specialPool": compute_special_pool(state, special),
        "raffles": {rid: {**robj, "pool": compute_raffle_pool(robj)} for rid, robj in raffles.items()},
    })

@app.route("/api/export", methods=["POST"])
def export_to_sheet():
    """Append all results to a 'SPIN RESULTS' tab in the Master sheet (creates it if missing)."""
    token = get_token()
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    HA = {"Authorization": f"Bearer {token}"}

    meta = requests.get(f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SHEET_ID}", headers=HA).json()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if "SPIN RESULTS" not in titles:
        requests.post(f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SHEET_ID}:batchUpdate",
            headers=H, json={"requests": [{"addSheet": {"properties": {"title": "SPIN RESULTS"}}}]})
        requests.post(f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SHEET_ID}/values/"
            f"{urllib.parse.quote('SPIN RESULTS!A1')}?valueInputOption=USER_ENTERED",
            headers=H, json={"values": [["TIMESTAMP", "NO.", "NAME", "WHEEL", "PRIZE"]]})

    results = get_results()
    rows = [[r["ts"], r["no"], r["name"], r["wheel"].upper(), r["prize_name"]] for r in results]
    for a in get_special()["awarded"]:
        rows.append([a["ts"], a["no"], a["name"], "SPECIAL RAFFLE", a["gift"]])
    for rid, robj in get_raffles().items():
        for a in robj["awarded"]:
            rows.append([a["ts"], "", a["name"], robj["label"].upper(), a["gift"]])
    requests.post(f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SHEET_ID}/values/"
        f"{urllib.parse.quote('SPIN RESULTS!A2')}:append?valueInputOption=USER_ENTERED&insertDataOption=OVERWRITE",
        headers=H, json={"values": rows})
    return jsonify({"exported": len(rows)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
