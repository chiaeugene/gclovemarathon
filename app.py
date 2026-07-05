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

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
MASTER_SHEET_ID = os.environ["MASTER_SHEET_ID"]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "gc520admin")

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
            {"id": "n1", "name": "RM10 Voucher", "qty": 20},
            {"id": "n2", "name": "RM20 Voucher", "qty": 15},
            {"id": "n3", "name": "Small Gift", "qty": 20},
            {"id": "n4", "name": "Try Again", "qty": 999},
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
        }
        rng = urllib.parse.quote(f"{APP_STATE_TAB}!A1")
        requests.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SHEET_ID}/values/{rng}"
            f"?valueInputOption=RAW", headers=H,
            json={"values": [["blob", json.dumps(payload)]]}, timeout=10)
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
    return jsonify({
        "state": get_state(),
        "prizes": get_prizes(),
        "results": get_results(),
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
    body = request.get_json()
    no = body.get("no")
    wheel = body.get("wheel")  # 'normal' or 'special'
    if wheel not in ("normal", "special"):
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
    pool = [p for p in prizes[wheel] if p["qty"] > 0]
    if not pool:
        return jsonify({"error": "no prizes left in this wheel"}), 400

    chosen = random.choice(pool)  # equal probability per remaining prize TYPE
    chosen["qty"] -= 1
    save_json(PRIZES_FILE, prizes)

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

    prizes = get_prizes()
    for p in prizes[r["wheel"]]:
        if p["id"] == r["prize_id"]:
            p["qty"] += 1
            break
    save_json(PRIZES_FILE, prizes)

    state = get_state()
    for p in state["participants"]:
        if p["no"] == no:
            p[f"{r['wheel']}_used"] = max(0, p[f"{r['wheel']}_used"] - 1)
            break
    save_json(STATE_FILE, state)
    cloud_backup()
    return jsonify({"undone": r})

@app.route("/api/reset", methods=["POST"])
def reset_event():
    """Full reset for dry-runs: every participant gets their spins back,
    prize quantities are restored from the baseline (whatever was last
    saved in Manage Prizes), and the results log is cleared. Requires the
    admin password so a stray click during the live event can't wipe it."""
    body = request.get_json(silent=True) or {}
    if body.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "wrong password"}), 403

    state = get_state()
    for p in state.get("participants", []):
        p["normal_used"] = 0
        p["special_used"] = 0
    save_json(STATE_FILE, state)

    baseline = get_prizes_baseline()
    save_json(PRIZES_FILE, json.loads(json.dumps(baseline)))

    save_json(RESULTS_FILE, [])
    cloud_backup()
    return jsonify({"state": state, "prizes": get_prizes(), "results": []})

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
    requests.post(f"https://sheets.googleapis.com/v4/spreadsheets/{MASTER_SHEET_ID}/values/"
        f"{urllib.parse.quote('SPIN RESULTS!A2')}:append?valueInputOption=USER_ENTERED&insertDataOption=OVERWRITE",
        headers=H, json={"values": rows})
    return jsonify({"exported": len(rows)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
