#!/usr/bin/env python3
"""
WORLD v1 — Toss Payments + Daily Tick + Public Log
Single-file server (Flask) with SQLite storage.

Endpoints
- GET  /              : purchase page (Payment Widget)
- GET  /log/latest    : public log text (ONLY public artifact)
- POST /cron/daily    : daily tick (cron-triggered, token protected)
- POST /order/create  : create order for BUFFER/AMP
- GET  /toss/success  : success redirect; server confirms payment, grants action
- GET  /toss/fail     : fail redirect

ENV
- PORT
- BASE_URL                 (e.g., https://xxx.onrender.com)
- CRON_TOKEN               (e.g., abc123...)
- TOSS_CLIENT_KEY          (public client key)
- TOSS_SECRET_KEY          (secret key for server confirm; must be Base64 Basic auth)
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import random
import sqlite3
import textwrap
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from flask import Flask, Response, jsonify, redirect, render_template_string, request

APP = Flask(__name__)

DB_PATH = os.environ.get("WORLD_DB", "world.db")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
CRON_TOKEN = os.environ.get("CRON_TOKEN", "")
TOSS_CLIENT_KEY = os.environ.get("TOSS_CLIENT_KEY", "")
TOSS_SECRET_KEY = os.environ.get("TOSS_SECRET_KEY", "")

# ----- WORLD v1 defaults (as agreed) -----
INIT_POP = 55
INIT_STB = 45
INIT_RISK = 50

# event weights when risk >= 80
W_COLLAPSE = 50
W_PROSPER = 20
W_CIVIL_WAR = 30

# chain event rule
CHAIN_PROB = 0.20
CHAIN_W_COLLAPSE = 70
CHAIN_W_PROSPER = 30

# extinction rule
EXT_THRESHOLD = 30
EXT_DAYS = 7
EXT_CANCEL_ON_RECOVERY = False  # fixed: no cancel

# items
BUFFER_PRICE = 19000
AMP_PRICE = 29000
CURRENCY = "KRW"

# item effects
BUFFER_RISK_REDUCE_PER_STACK = 5   # applied before tick, stacks persist
AMP_MULT = 1.2                     # consumes 1 charge on event application
AMP_APPLY_CIVIL_WAR_ONLY_FIRST_DAY = True

# security headers for minimal hardening
SEC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self' https://js.tosspayments.com; "
                              "script-src 'self' https://js.tosspayments.com; "
                              "style-src 'self' 'unsafe-inline'; "
                              "connect-src 'self'; "
                              "frame-ancestors 'none';",
}

# ----- DB helpers -----

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = db()
    cur = conn.cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS world_state(
        id INTEGER PRIMARY KEY CHECK (id=1),
        day INTEGER NOT NULL,
        pop INTEGER NOT NULL,
        stb INTEGER NOT NULL,
        risk INTEGER NOT NULL,
        civil_war_until_day INTEGER NOT NULL,
        ext_start_day INTEGER NOT NULL,
        last_tick_utc_date TEXT NOT NULL
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS actions(
        action TEXT PRIMARY KEY,
        count INTEGER NOT NULL
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS ticks(
        day INTEGER PRIMARY KEY,
        pop INTEGER NOT NULL,
        stb INTEGER NOT NULL,
        risk INTEGER NOT NULL,
        pop_delta INTEGER NOT NULL,
        stb_delta INTEGER NOT NULL,
        risk_delta INTEGER NOT NULL,
        event TEXT NOT NULL,
        ts_utc TEXT NOT NULL,
        extra_event TEXT NOT NULL
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS orders(
        order_id TEXT PRIMARY KEY,
        action TEXT NOT NULL,
        amount INTEGER NOT NULL,
        status TEXT NOT NULL,
        created_ts_utc TEXT NOT NULL,
        payment_key TEXT
    )""")

    # seed
    cur.execute("""INSERT OR IGNORE INTO world_state
        (id, day, pop, stb, risk, civil_war_until_day, ext_start_day, last_tick_utc_date)
        VALUES(1, 0, ?, ?, ?, 0, 0, '')""", (INIT_POP, INIT_STB, INIT_RISK))

    cur.execute("INSERT OR IGNORE INTO actions(action, count) VALUES('BUFFER', 0)")
    cur.execute("INSERT OR IGNORE INTO actions(action, count) VALUES('AMP', 0)")

    conn.commit()
    conn.close()

# ---- ensure DB initialized on import (Render safe) ----
try:
    init_db()
except Exception as e:
    print("DB init skipped:", e)

def clamp(x: int, lo: int = 0, hi: int = 100) -> int:
    return max(lo, min(hi, x))

def utc_date_str() -> str:
    return dt.datetime.utcnow().date().isoformat()

def sha256_int(s: str) -> int:
    return int(hashlib.sha256(s.encode("utf-8")).hexdigest(), 16)

def set_headers(resp):
    for k, v in SEC_HEADERS.items():
        resp.headers[k] = v
    return resp

def get_actions(conn: sqlite3.Connection) -> Dict[str, int]:
    cur = conn.cursor()
    cur.execute("SELECT action, count FROM actions")
    return {r["action"]: int(r["count"]) for r in cur.fetchall()}

def grant_action(conn: sqlite3.Connection, action: str, n: int = 1) -> None:
    if action not in ("BUFFER", "AMP"):
        raise ValueError("bad action")
    cur = conn.cursor()
    cur.execute("UPDATE actions SET count = count + ? WHERE action = ?", (int(n), action))

def consume_amp(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT count FROM actions WHERE action='AMP'")
    c = int(cur.fetchone()[0])
    if c > 0:
        cur.execute("UPDATE actions SET count = count - 1 WHERE action='AMP'")
        return True
    return False

def buffer_reduce(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("SELECT count FROM actions WHERE action='BUFFER'")
    stacks = int(cur.fetchone()[0])
    return stacks * BUFFER_RISK_REDUCE_PER_STACK

def choose_weighted(rng: random.Random, weights: Dict[str, int]) -> str:
    total = sum(weights.values())
    r = rng.randint(1, total)
    acc = 0
    for k, w in weights.items():
        acc += w
        if r <= acc:
            return k
    return list(weights.keys())[-1]

def apply_event(pop: int, stb: int, risk: int, event: str, amp_on: bool) -> Tuple[int, int, int]:
    mul = AMP_MULT if amp_on else 1.0
    if event == "COLLAPSE":
        return pop - round(15 * mul), stb - round(20 * mul), risk + round(15 * mul)
    if event == "PROSPER":
        return pop + round(12 * mul), stb + round(18 * mul), risk - round(8 * mul)
    if event == "CIVIL_WAR":
        return pop - round(8 * mul), stb - round(15 * mul), risk + round(10 * mul)
    return pop, stb, risk

def maybe_reset_on_extinction(conn: sqlite3.Connection, next_day: int) -> bool:
    """Return True if reset occurred."""
    cur = conn.cursor()
    cur.execute("SELECT ext_start_day FROM world_state WHERE id=1")
    ext_start = int(cur.fetchone()[0])

    if ext_start <= 0:
        return False

    if next_day >= ext_start + EXT_DAYS:
        # reset to DAY0 and clear ext, civil war, ticks remain (history preserved)
        cur.execute("""UPDATE world_state SET
            day=0, pop=?, stb=?, risk=?, civil_war_until_day=0, ext_start_day=0
            WHERE id=1""", (INIT_POP, INIT_STB, INIT_RISK))
        return True
    return False

def tick_once() -> Dict[str, object]:
    conn = db()
    cur = conn.cursor()

    # duplicate-run guard by UTC date
    cur.execute("SELECT last_tick_utc_date FROM world_state WHERE id=1")
    last_date = cur.fetchone()[0]
    today = utc_date_str()
    if last_date == today:
        conn.close()
        return {"ok": False, "reason": "already_ran_today", "utc_date": today}

    cur.execute("""SELECT day, pop, stb, risk, civil_war_until_day, ext_start_day
                   FROM world_state WHERE id=1""")
    s = cur.fetchone()
    day = int(s["day"])
    pop = int(s["pop"])
    stb = int(s["stb"])
    risk = int(s["risk"])
    cw_until = int(s["civil_war_until_day"])
    ext_start = int(s["ext_start_day"])

    next_day = day + 1

    # deterministic per-day RNG seed
    seed = sha256_int(f"WORLDv1:{next_day}")
    rng = random.Random(seed)

    # apply BUFFER before tick
    risk = clamp(risk - buffer_reduce(conn))

    # base update (float, then round)
    pop_delta_f = rng.randint(-3, 5) - (risk / 25.0)
    stb_delta_f = rng.randint(-4, 4) + (pop / 50.0)
    risk_delta_f = rng.randint(-2, 6) - (stb / 40.0)

    base_pop = clamp(int(round(pop + pop_delta_f)))
    base_stb = clamp(int(round(stb + stb_delta_f)))
    base_risk = clamp(int(round(risk + risk_delta_f)))

    event = "NONE"
    extra_event = "NONE"

    # civil war ongoing?
    if cw_until >= next_day:
        event = "CIVIL_WAR"
        amp_on = False
        if AMP_APPLY_CIVIL_WAR_ONLY_FIRST_DAY:
            # apply amp only if this is the first day of the war (cw_until == next_day+2)
            if cw_until == next_day + 2:
                amp_on = consume_amp(conn)
        else:
            amp_on = consume_amp(conn)

        new_pop, new_stb, new_risk = apply_event(base_pop, base_stb, base_risk, "CIVIL_WAR", amp_on)
    else:
        # trigger if risk >= 80
        if base_risk >= 80:
            event = choose_weighted(rng, {"COLLAPSE": W_COLLAPSE, "PROSPER": W_PROSPER, "CIVIL_WAR": W_CIVIL_WAR})
            amp_on = consume_amp(conn)
            new_pop, new_stb, new_risk = apply_event(base_pop, base_stb, base_risk, event, amp_on)
            if event == "CIVIL_WAR":
                cw_until = next_day + 2  # 3 days total inclusive
        else:
            new_pop, new_stb, new_risk = base_pop, base_stb, base_risk

    # chain event: if stb <= 20 after main tick, roll 20% for extra (no civil war)
    if event != "CIVIL_WAR" and new_stb <= 20:
        if rng.random() < CHAIN_PROB:
            extra_event = choose_weighted(rng, {"COLLAPSE": CHAIN_W_COLLAPSE, "PROSPER": CHAIN_W_PROSPER})
            amp_on = consume_amp(conn)
            new_pop, new_stb, new_risk = apply_event(new_pop, new_stb, new_risk, extra_event, amp_on)

    new_pop, new_stb, new_risk = clamp(int(new_pop)), clamp(int(new_stb)), clamp(int(new_risk))

    # extinction countdown start
    if new_pop <= EXT_THRESHOLD and ext_start <= 0:
        ext_start = next_day

    # save tick record (deltas vs pre-buffer risk? we keep deltas vs previous visible state)
    pop_delta = int(new_pop - pop)
    stb_delta = int(new_stb - stb)
    risk_delta = int(new_risk - int(s["risk"]))  # compared to stored risk before buffer

    cur.execute("""INSERT OR REPLACE INTO ticks
        (day, pop, stb, risk, pop_delta, stb_delta, risk_delta, event, ts_utc, extra_event)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (next_day, new_pop, new_stb, new_risk, pop_delta, stb_delta, risk_delta,
         event, dt.datetime.utcnow().isoformat(timespec="seconds") + "Z", extra_event)
    )

    cur.execute("""UPDATE world_state SET
        day=?, pop=?, stb=?, risk=?, civil_war_until_day=?, ext_start_day=?, last_tick_utc_date=?
        WHERE id=1""", (next_day, new_pop, new_stb, new_risk, cw_until, ext_start, today))

    # apply extinction reset if due (after state update so tick is recorded)
    reset = maybe_reset_on_extinction(conn, next_day)

    conn.commit()
    actions = get_actions(conn)
    conn.close()

    return {
        "ok": True,
        "day": next_day,
        "event": event,
        "extra_event": extra_event,
        "reset": bool(reset),
        "actions": actions,
        "utc_date": today,
    }

# ----- Toss Payments -----

def toss_basic_auth() -> str:
    # Toss uses Basic auth: base64(secretKey + ":")
    raw = f"{TOSS_SECRET_KEY}:".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")

def toss_confirm(payment_key: str, order_id: str, amount: int) -> Tuple[bool, str]:
    url = "https://api.tosspayments.com/v1/payments/confirm"
    body = json.dumps({"paymentKey": payment_key, "orderId": order_id, "amount": amount}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", "Basic " + toss_basic_auth())
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read().decode("utf-8")
            return True, data
    except urllib.error.HTTPError as e:
        try:
            err = e.read().decode("utf-8")
        except Exception:
            err = str(e)
        return False, err
    except Exception as e:
        return False, str(e)

# ----- Views -----

INDEX_HTML = r"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>WORLD v1</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:24px;max-width:720px}
    .card{border:1px solid #ddd;border-radius:12px;padding:16px;margin:12px 0}
    button{padding:12px 14px;border-radius:10px;border:1px solid #111;background:#111;color:#fff;cursor:pointer}
    button.secondary{background:#fff;color:#111}
    small{color:#666}
    pre{white-space:pre-wrap;word-break:break-word;background:#f7f7f7;padding:12px;border-radius:10px}
    .row{display:flex;gap:10px;flex-wrap:wrap}
    .row button{flex:1;min-width:160px}
  </style>
  <script src="https://js.tosspayments.com/v1/payment-widget"></script>
</head>
<body>
  <h1>WORLD v1</h1>

  <div class="card">
    <div class="row">
      <button onclick="buy('BUFFER')">완충권 ₩19,000</button>
      <button onclick="buy('AMP')" class="secondary">증폭권 ₩29,000</button>
    </div>
    <p><small>환불 없음. 결과 기록 영구.</small></p>
  </div>

  <div class="card">
    <p><a href="/log/latest">/log/latest</a></p>
    <pre id="log">(loading...)</pre>
  </div>

<script>
const clientKey = "{{ client_key }}";
const baseUrl = "{{ base_url }}";

async function buy(action){
  const res = await fetch("/order/create", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({action})
  });
  const data = await res.json();
  if(!data.ok){ alert("order error"); return; }

  const paymentWidget = PaymentWidget(clientKey, data.customerKey);
  paymentWidget.requestPayment({
    orderId: data.orderId,
    orderName: data.orderName,
    amount: data.amount,
    successUrl: baseUrl + "/toss/success",
    failUrl: baseUrl + "/toss/fail"
  });
}

async function loadLog(){
  const r = await fetch("/log/latest", {cache:"no-store"});
  document.getElementById("log").textContent = await r.text();
}
loadLog();
</script>

</body>
</html>
"""

@APP.after_request
def _after(resp):
    return set_headers(resp)

@APP.get("/")
def index():
    # require BASE_URL + TOSS_CLIENT_KEY in production
    return render_template_string(INDEX_HTML, client_key=TOSS_CLIENT_KEY, base_url=BASE_URL or request.host_url.rstrip("/"))

@APP.get("/log/latest")
def log_latest():
    conn = db()
    cur = conn.cursor()
    cur.execute("""SELECT day,pop,stb,risk,event,pop_delta,stb_delta,risk_delta,extra_event
                   FROM ticks ORDER BY day DESC LIMIT 1""")
    t = cur.fetchone()
    if not t:
        cur.execute("SELECT day,pop,stb,risk FROM world_state WHERE id=1")
        s = cur.fetchone()
        day = int(s["day"])
        pop = int(s["pop"])
        stb = int(s["stb"])
        risk = int(s["risk"])
        event = "NONE"
        pop_d = stb_d = risk_d = 0
        extra = "NONE"
    else:
        day = int(t["day"])
        pop = int(t["pop"])
        stb = int(t["stb"])
        risk = int(t["risk"])
        event = str(t["event"])
        pop_d = int(t["pop_delta"])
        stb_d = int(t["stb_delta"])
        risk_d = int(t["risk_delta"])
        extra = str(t["extra_event"])

    actions = get_actions(conn)
    conn.close()

    # if chain happened, still keep EVENT line as main event; extra is not shown (numbers only policy).
    text = (
        f"DAY {day:03d}\n"
        f"POP {pop} ({pop_d:+d})\n"
        f"STB {stb} ({stb_d:+d})\n"
        f"RISK {risk} ({risk_d:+d})\n"
        f"EVENT: {event}\n"
        f"TOP ACTIONS: BUFFER x{actions.get('BUFFER',0)} / AMP x{actions.get('AMP',0)}\n"
    )
    return Response(text, mimetype="text/plain; charset=utf-8")

@APP.post("/cron/daily")
def cron_daily():
    if not CRON_TOKEN or request.headers.get("X-CRON-TOKEN") != CRON_TOKEN:
        return jsonify({"ok": False}), 403
    out = tick_once()
    status = 200 if out.get("ok") else 409
    return jsonify(out), status

@APP.post("/order/create")
def order_create():
    data = request.get_json(force=True, silent=True) or {}
    action = (data.get("action") or "").upper().strip()
    if action not in ("BUFFER", "AMP"):
        return jsonify({"ok": False}), 400

    amount = BUFFER_PRICE if action == "BUFFER" else AMP_PRICE
    order_id = "W1-" + hashlib.sha256(f"{dt.datetime.utcnow().isoformat()}:{random.random()}".encode()).hexdigest()[:20]
    order_name = "완충권" if action == "BUFFER" else "증폭권"
    customer_key = "CUST-" + hashlib.sha256(request.remote_addr.encode() if request.remote_addr else b"0").hexdigest()[:16]

    conn = db()
    cur = conn.cursor()
    cur.execute("""INSERT INTO orders(order_id, action, amount, status, created_ts_utc)
                   VALUES(?,?,?,?,?)""",
                (order_id, action, amount, "CREATED", dt.datetime.utcnow().isoformat(timespec="seconds")+"Z"))
    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "orderId": order_id,
        "orderName": order_name,
        "amount": amount,
        "currency": CURRENCY,
        "customerKey": customer_key,
    })

@APP.get("/toss/success")
def toss_success():
    payment_key = request.args.get("paymentKey", "")
    order_id = request.args.get("orderId", "")
    amount_str = request.args.get("amount", "")

    try:
        amount = int(amount_str)
    except Exception:
        return "bad amount", 400

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT action, amount, status FROM orders WHERE order_id=?", (order_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return "order not found", 404
    if int(row["amount"]) != amount:
        conn.close()
        return "amount mismatch", 400
    if str(row["status"]) == "CONFIRMED":
        # already confirmed: idempotent
        conn.close()
        return redirect("/log/latest")

    # confirm with Toss
    if not TOSS_SECRET_KEY:
        conn.close()
        return "server missing TOSS_SECRET_KEY", 500

    ok, payload = toss_confirm(payment_key, order_id, amount)
    if not ok:
        # keep for debugging (not public)
        cur.execute("UPDATE orders SET status=?, payment_key=? WHERE order_id=?",
                    ("FAILED_CONFIRM", payment_key, order_id))
        conn.commit()
        conn.close()
        return "payment confirm failed", 400

    action = str(row["action"])
    # grant action + mark confirmed
    grant_action(conn, action, 1)
    cur.execute("UPDATE orders SET status=?, payment_key=? WHERE order_id=?",
                ("CONFIRMED", payment_key, order_id))
    conn.commit()
    conn.close()

    # policy: do not show explanation; go to log
    return redirect("/log/latest")

@APP.get("/toss/fail")
def toss_fail():
    # Keep minimal surface; do not disclose details publicly.
    return "PAYMENT FAILED", 400

# Optional: local admin test endpoint (disable in production by not setting ADMIN_TOKEN)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
@APP.post("/admin/grant")
def admin_grant():
    if not ADMIN_TOKEN or request.headers.get("X-ADMIN-TOKEN") != ADMIN_TOKEN:
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True, silent=True) or {}
    action = (data.get("action") or "").upper().strip()
    n = int(data.get("n") or 1)
    conn = db()
    grant_action(conn, action, n)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

def main():
    init_db()
    port = int(os.environ.get("PORT", "8080"))
    APP.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
