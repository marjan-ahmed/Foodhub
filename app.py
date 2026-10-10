#!/usr/bin/env python3
"""
=====================================================================
  FoodHub — WhatsApp Restaurant Order Bot  (engine, config-driven)
=====================================================================
WHAT IT DOES
  A customer WhatsApps the restaurant's number. The bot takes the
  order (English / Roman Urdu), confirms the bill, sends a ticket to
  the kitchen screen, and updates the customer as the chef taps
  Preparing -> Ready. Staff only touch the weird cases.

  One engine, many restaurants: every restaurant is a JSON config
  (menu, prices, variants, owner number). No code changes per client.

RUN IT
  pip install -r requirements.txt
  python app.py                                  # demo restaurant
  python app.py --new                            # scaffold a real restaurant
  python app.py --check restaurants/example.json # validate a config
  RESTAURANT_CONFIG=restaurants/karachi-foods.json python app.py

PAGES
  /          landing + setup guide
  /demo      simulated WhatsApp chat (try it with NO setup at all)
  /kitchen   live kitchen ticket screen (open on a tablet)

ONBOARDING A NEW RESTAURANT (the 48-hour playbook)
  1. Collect the PKR 35k setup fee upfront.
  2. python app.py --new  -> answer prompts -> restaurants/<slug>.json
  3. Get a spare SIM from the owner; register it as the bot's number
     in the Meta dashboard (Step 2: Production setup, OTP via call/SMS).
  4. Deploy: RESTAURANT_CONFIG=... WA_TOKEN=... WA_PHONE_ID=...
     WA_VERIFY_TOKEN=... WA_APP_SECRET=... DEMO_MODE=0 python app.py
     (webhook -> https://<url>/webhook; WA_APP_SECRET enables signature
     verification, DEMO_MODE=0 disables the demo endpoints for production)
  5. Kitchen tablet open at /kitchen. Test an order together with the owner.
  6. Go live: bot number + QR code on menu, flyers, Google profile.

DESIGN NOTES
  - The LLM understands, templates talk. Customer-facing wording comes
    from fixed templates (T dict) so replies stay tight and consistent.
  - Deterministic fast-path first (yes/no/greetings/owner commands) so
    most messages never touch the LLM -> instant + nearly free.
  - Totals are computed in Python. The model never does arithmetic and
    can never invent a menu item (ids are validated against the config).
=====================================================================
"""

import os
import re
import sys
import json
import time
import html
import hmac
import hashlib
import difflib
from collections import deque
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

try:
    import requests as http
except ImportError:                      # demo/simulator works without it
    http = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    """Load KEY=VALUE lines from a .env file (if present) into os.environ.
    Real environment variables always win over the file."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass


_load_dotenv(os.path.join(BASE_DIR, ".env"))

# ----------------------------- Config ------------------------------
VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "demo123")
WA_TOKEN     = os.getenv("WA_TOKEN", "")        # Meta Cloud API bearer token
WA_PHONE_ID  = os.getenv("WA_PHONE_ID", "")     # phone_number_id from dashboard
WA_APP_SECRET = os.getenv("WA_APP_SECRET", "")  # Meta app secret (webhook signatures)
OWNER_PHONE  = os.getenv("OWNER_PHONE", "")     # fallback if not in config
DEMO_MODE    = os.getenv("DEMO_MODE", "1") == "1"  # /demo + /api/sim enabled
MAX_QTY      = int(os.getenv("MAX_QTY", "20"))  # per-line quantity cap
LLM_API_KEY  = os.getenv("LLM_API_KEY", "")     # optional enhancement
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL    = os.getenv("LLM_MODEL", "gpt-4o-mini")
PORT         = int(os.getenv("PORT", "8000"))
CONFIG_PATH  = os.getenv("RESTAURANT_CONFIG") or os.path.join(
    BASE_DIR, "restaurants", "demo.json")


def validate_config(cfg):
    """Return a list of problems with a restaurant config (empty = valid)."""
    errs = []
    if not cfg.get("name"):
        errs.append("missing 'name'")
    menu = cfg.get("menu") or []
    if not menu:
        errs.append("menu is empty")
    ids = [m.get("id") for m in menu]
    if len(ids) != len(set(ids)):
        errs.append("duplicate menu ids")
    for m in menu:
        if not m.get("id"):
            errs.append("a menu item is missing its id")
        if not m.get("name"):
            errs.append(f"item '{m.get('id')}': missing name")
        if not isinstance(m.get("price"), int) or m["price"] <= 0:
            errs.append(f"item '{m.get('id')}': price must be a positive integer")
    for c in cfg.get("clarify", []):
        for o in c.get("options", []):
            if o.get("id") not in ids:
                errs.append(
                    f"clarify '{c.get('key')}': option id '{o.get('id')}' "
                    f"is not on the menu")
    return errs


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    errs = validate_config(cfg)
    if errs:
        raise ValueError("Invalid restaurant config:\n- " + "\n- ".join(errs))
    return cfg


def blank_config():
    return {"name": "FoodHub", "slug": "demo", "owner_phone": "",
            "currency": "Rs", "eta": "35-45", "menu": [], "clarify": []}


SCAFFOLD_MODE = "--new" in sys.argv or "--check" in sys.argv
if os.path.exists(CONFIG_PATH):
    CFG = load_config(CONFIG_PATH)
elif SCAFFOLD_MODE:
    CFG = blank_config()          # --new / --check don't need a live config
else:
    raise SystemExit(
        f"Restaurant config not found: {CONFIG_PATH}\n"
        f"Run: python app.py --new   (to create one)")

# ------------------------- Derived from config ---------------------
BRAND = CFG.get("name", "FoodHub")
MENU = CFG.get("menu", [])
for m in MENU:
    m.setdefault("available", True)
    m.setdefault("aliases", [m["name"].lower()])

# Words that need a follow-up question before they can be ordered.
# {key: (display name, [(item_id, label), ...])}
CLARIFY = {}
CLARIFY_ALIASES = {}
for c in CFG.get("clarify", []):
    CLARIFY[c["key"]] = (
        c["name"], [(o["id"], o["label"]) for o in c["options"]])
    for a in c.get("aliases", [c["key"]]):
        CLARIFY_ALIASES[a.lower()] = c["key"]

NUMBER_WORDS = {"ek": 1, "aik": 1, "do": 2, "teen": 3, "char": 4,
                "chaar": 4, "paanch": 5, "one": 1, "two": 2,
                "three": 3, "four": 4, "five": 5, "six": 6}

YES = {"yes", "y", "ok", "okay", "done", "confirm", "haan", "han",
       "jee", "ji", "theek", "theek hai", "yes plz", "yes please", "krdo", "kar do"}
NO = {"no", "n", "nahi", "nahin", "cancel", "radd", "rehnedo", "rehne do"}
GREET = {"salam", "assalam", "assalam-o-alaikum", "assalam o alaikum",
         "hello", "hi", "aoa", "hey"}
MENU_WORDS = {"menu", "menue", "rate", "rates", "price", "prices",
              "price list", "menu bhejo", "menu dikhao"}
COMPLAINT_WORDS = {"wrong", "ghalat", "galat", "late", "der", "complaint",
                   "shikayat", "cold", "thanda", "kharab", "missing"}
FILLER = {"please", "plz", "aur", "and", "ek", "aik", "with", "ka", "ki",
          "ko", "mein", "mai", "bhejo", "bhej", "dein", "den", "order",
          "krna", "karna", "chahiye", "hai", "he", "kae", "ke", "ki"}

# --------------------------- Templates -----------------------------
# Every customer-facing sentence lives here. The LLM never writes
# free text to customers — it only fills these slots (see llm_parse).
T = {
    "en": {
        "greeting": "Hi! Welcome to {brand}. Send your order like this: '2 zinger, 1 coke'. Type MENU for the full menu.",
        "menu_header": "Today's menu:",
        "menu_line": "- {name}: Rs {price}",
        "menu_gone": " (finished)",
        "bill": "{lines}\nTotal: Rs {total}\nDeliver to {addr}? Reply YES to confirm, or send your full address.",
        "bill_no_addr": "{lines}\nTotal: Rs {total}\nWhat's your delivery address? (block / street / area + landmark)",
        "ask_address": "What's your delivery address? (block / street / area + any landmark)",
        "confirmed": "Done! Order #{oid} confirmed — about {eta} mins, cash on delivery. I'll message you when the rider leaves.",
        "clarify": "Got it — {name}: {options}?",
        "unknown": "Hmm, we don't have '{word}'. Did you mean: {suggest}?",
        "unknown_none": "Sorry, I didn't catch that. Try '2 zinger, 1 coke' — or type MENU.",
        "finished_now": "Sorry — {name} is finished right now. Anything else from the menu?",
        "cancelled": "No problem — order cancelled. Send a new order whenever you're ready.",
        "nothing_to_confirm": "I don't have an order to confirm right now — send your order first!",
        "handoff": "Sorry about that — getting someone for you now, one moment.",
        "finished_set": "{name} marked as finished — I won't sell it till you say available.",
        "available_set": "{name} is back on the menu!",
        "not_on_menu": "Can't find '{word}' on the menu.",
        "today": "Today: {n} orders, Rs {rev} revenue.",
        "owner_menu_hint": "Owner mode. Commands: MENU | '<item> finished' | '<item> available' | TODAY | 'bot off' | 'bot on'",
        "ticket_preparing": "Good news — order #{oid} is being prepared!",
        "ticket_ready": "Order #{oid} is on its way! The rider has left.",
        "big_order": "That's a large order — let me get the restaurant to confirm it personally, one moment.",
        "bot_disabled": "Sorry — we're not taking online orders right now. Please call the restaurant directly.",
        "bot_off": "Bot disabled — customers get the 'closed' reply until you say 'bot on'.",
        "bot_on": "Bot enabled — taking orders again.",
    },
    "ur": {
        "greeting": "Assalam-o-Alaikum! {brand} mein khush aamdeed. Order is tarah bhejein: '2 zinger, 1 coke'. Mukammal menu ke liye MENU likhein.",
        "menu_header": "Aaj ka menu:",
        "menu_line": "- {name}: Rs {price}",
        "menu_gone": " (khatam)",
        "bill": "{lines}\nTotal: Rs {total}\n{addr} par deliver karun? Confirm ke liye YES likhein, ya apna mukammal address bhej dein.",
        "bill_no_addr": "{lines}\nTotal: Rs {total}\nApna delivery address kya hai? (block / street / area + landmark)",
        "ask_address": "Apna delivery address bata dein? (block / street / area + landmark)",
        "confirmed": "Ho gaya! Order #{oid} confirm — taqreeban {eta} min, cash on delivery. Rider nikalte hi message karunga.",
        "clarify": "Theek hai — {name}: {options}?",
        "unknown": "Hmm, '{word}' hamare paas nahi hai. Kya aap ka matlab tha: {suggest}?",
        "unknown_none": "Maazrat, samajh nahi aaya. '2 zinger, 1 coke' try karein — ya MENU likhein.",
        "finished_now": "Maazrat — {name} abhi khatam hai. Menu se kuch aur?",
        "cancelled": "Koi baat nahi — order cancel ho gaya. Jab chahen naya order bhej dein.",
        "nothing_to_confirm": "Abhi confirm karne ke liye koi order nahi — pehle apna order bhejein!",
        "handoff": "Maazrat — abhi kisi ko bulata hun, ek minute.",
        "finished_set": "{name} khatam mark kar diya — jab tak aap 'available' na kahein, nahi bechunga.",
        "available_set": "{name} wapas menu mein hai!",
        "not_on_menu": "'{word}' menu mein nahi mila.",
        "today": "Aaj: {n} orders, Rs {rev} sale.",
        "owner_menu_hint": "Owner mode. Commands: MENU | '<item> finished' | '<item> available' | TODAY | 'bot off' | 'bot on'",
        "ticket_preparing": "Khushkhabri — order #{oid} tayyar ho raha hai!",
        "ticket_ready": "Order #{oid} nikal gaya! Rider rawana ho chuka hai.",
        "big_order": "Ye bara order hai — restaurant se confirm karwa kar batata hun, ek minute.",
        "bot_disabled": "Maazrat — hum abhi online order nahi le rahe. Barah-e-karam restaurant ko call karein.",
        "bot_off": "Bot band kar diya — 'bot on' kehne tak customers ko 'closed' reply milega.",
        "bot_on": "Bot chalu kar diya — orders le rahe hain.",
    },
}


# Currency comes from the restaurant config (default Rs).
CUR = (CFG.get("currency") or "Rs").strip() or "Rs"
if CUR != "Rs":
    for _lang in T:
        for _k in T[_lang]:
            T[_lang][_k] = T[_lang][_k].replace("Rs", CUR)


def tr(lang: str, key: str, **kw) -> str:
    """Render a template, injecting the restaurant brand automatically."""
    kw.setdefault("brand", BRAND)
    return T[lang][key].format(**kw)

# --------------------------- Runtime state -------------------------
# MVP: in-memory. Pilot: move these to Postgres (same shapes).
sessions  = {}   # phone -> {"state":..,"pending":[(item,qty)],"address":..,"lang":..,"clarify_for":..}
customers = {}   # phone -> {"address": str|None, "orders": int}
tickets   = []   # kitchen tickets, newest last
order_seq = 1041

ALIAS_MAP = {}
for m in MENU:
    for a in m["aliases"]:
        ALIAS_MAP[a] = m
ALL_ALIAS_WORDS = sorted({w for a in ALIAS_MAP for w in a.split()})

# --------------------------- Helpers -------------------------------
def norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def detect_lang(text: str) -> str:
    if re.search(r"[\u0600-\u06FF]", text or ""):
        return "ur"
    words = set(norm(text).split())
    hits = {"hai", "ka", "ki", "ko", "mein", "mai", "chahiye", "bhejo",
            "bhej", "karo", "kar", "mera", "meri", "apka", "aapka", "wala",
            "wali", "aur", "nahi", "nahin", "salam", "shukriya", "kitne",
            "kya", "mujhe", "mujhay", "theek", "haan", "jee", "khatam"}
    return "ur" if len(words & hits) >= 2 else "en"


def get_session(phone: str) -> dict:
    s = sessions.get(phone)
    if not s:
        s = {"state": "idle", "pending": [], "address": None,
             "lang": "en", "clarify_for": None}
        saved = customers.get(phone, {})
        if saved.get("address"):
            s["address"] = saved["address"]
        sessions[phone] = s
    return s


def menu_text(lang: str) -> str:
    lines = [T[lang]["menu_header"]]
    for m in MENU:
        gone = "" if m["available"] else T[lang]["menu_gone"]
        lines.append(T[lang]["menu_line"].format(
            name=m["name"], price=f"{m['price']:,}") + gone)
    return "\n".join(lines)


def bill_lines(items, lang: str) -> str:
    return "\n".join(
        f"{q}x {m['name']} ({m['price']:,})" for m, q in items)


def bill_total(items) -> int:
    return sum(m["price"] * q for m, q in items)


# --------------------------- Order parsing -------------------------
def find_items(text: str):
    """Deterministic item extraction.
    Returns (items, clarify_hits, unavailable, leftover).
    items = [(menu_item, qty)]; clarify_hits = [clarify_key];
    unavailable = [item names matched but finished];
    leftover = unmatched text (often an address fragment)."""
    t = " " + norm(text) + " "
    items, spans, unavailable = [], [], []
    qty_words = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
    for alias in sorted(ALIAS_MAP, key=len, reverse=True):
        pat = re.compile(
            r"(?:(\d+|" + qty_words + r")\s+)?" + re.escape(alias) +
            r"(?:\s+(\d+|" + qty_words + r"))?")
        for mm in pat.finditer(t):
            s, e = mm.span()
            if any(s < ue and e > us for us, ue in spans):
                continue
            q = mm.group(1) or mm.group(2) or "1"
            qty = int(q) if q.isdigit() else NUMBER_WORDS.get(q, 1)
            m = ALIAS_MAP[alias]
            spans.append((s, e))
            if not m["available"]:
                if m["name"] not in unavailable:
                    unavailable.append(m["name"])
                continue
            items.append((m, qty))
    clarify_hits = []
    for calias, key in CLARIFY_ALIASES.items():
        if re.search(r"\b" + re.escape(calias) + r"\b", t):
            # skip if a specific variant was already matched
            if not any(m["id"].startswith(key) for m, _ in items):
                clarify_hits.append(key)
    # leftover text with matched spans blanked out -> address hint
    chars = list(t)
    for s, e in spans:
        for i in range(s, e):
            chars[i] = " "
    rest = re.sub(r"[^a-z0-9\s]", " ", "".join(chars))
    words = [w for w in rest.split()
             if w not in FILLER and (len(w) > 1 or w.isdigit())]
    leftover = " ".join(words).strip()
    return items, clarify_hits, unavailable, leftover


def suggest_for(word: str):
    cands = difflib.get_close_matches(word, ALL_ALIAS_WORDS, n=3, cutoff=0.72)
    names = []
    for c in cands:
        m = ALIAS_MAP.get(c)
        if m and m["available"] and m["name"] not in names:
            names.append(f"{m['name']} (Rs {m['price']:,})")
    return names[:2]


def unknown_suggestion(text: str, lang: str):
    words = [w for w in norm(text).split()
             if w not in FILLER and not w.isdigit() and len(w) > 2]
    for w in words:
        s = suggest_for(w)
        if s:
            return T[lang]["unknown"].format(word=w, suggest=", ".join(s))
    return None


def llm_parse(text: str, lang: str):
    """Optional enhancement: ask an LLM for hard cases.
    Returns (items, clarify_hits) or None if unavailable/failed."""
    if not LLM_API_KEY or http is None:
        return None
    menu_desc = "\n".join(
        f"- id:{m['id']} name:{m['name']} price:{m['price']} "
        f"aliases:{', '.join(m['aliases'])}" for m in MENU if m["available"])
    prompt = (
        "You parse a restaurant order message into JSON. Reply with ONLY JSON, "
        "no other text.\nSchema: {\"items\": [{\"id\": \"<menu id>\", \"qty\": <int>}], "
        "\"unknown\": [\"<words you could not map>\"]}\n"
        "Rules: map misspellings and Roman Urdu to the closest menu id. "
        "Never invent ids. qty defaults to 1.\n"
        f"MENU:\n{menu_desc}\nMESSAGE: {text}")
    try:
        r = http.post(
            LLM_BASE_URL.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0, "max_tokens": 300},
            timeout=12)
        data = json.loads(r.json()["choices"][0]["message"]["content"])
        items = []
        for it in data.get("items", []):
            m = next((x for x in MENU if x["id"] == it.get("id")), None)
            if m and m["available"]:
                items.append((m, max(1, int(it.get("qty", 1)))))
        return items, [], [], ""
    except Exception:
        return None


# --------------------------- Core handler --------------------------
def is_owner(phone: str) -> bool:
    # "__owner__" is the demo-simulator sentinel. It can only arrive via
    # /api/sim, which is gated behind DEMO_MODE, and the webhook refuses it
    # outright — so it can never impersonate the owner in production.
    if phone == "__owner__":
        return True
    owner = (CFG.get("owner_phone") or OWNER_PHONE or "").strip()
    return bool(owner) and phone == owner


def owner_command(phone: str, text: str, lang: str):
    t = norm(text)
    if t in ("menu", "menue"):
        return [menu_text(lang)]
    if t in ("today", "sales", "hisab", "aaj"):
        today = datetime.now().date().isoformat()
        todays = [o for o in tickets if o["date"] == today]
        rev = sum(o["total"] for o in todays)
        return [T[lang]["today"].format(n=len(todays), rev=f"{rev:,}")]
    if t in ("bot off", "bot stop", "stop bot", "bot band", "band karo"):
        CFG["bot_enabled"] = False          # in-memory; resets on restart
        return [T[lang]["bot_off"]]
    if t in ("bot on", "bot start", "start bot", "bot chalu", "chalu karo"):
        CFG["bot_enabled"] = True
        return [T[lang]["bot_on"]]
    mfin = re.match(r"(.+?)\s+(finished|khatam)\s*$", t)
    if mfin:
        return [toggle_item(mfin.group(1), False, lang)]
    mav = re.match(r"(.+?)\s+(available|wapas|start)\s*$", t)
    if mav:
        return [toggle_item(mav.group(1), True, lang)]
    return None


def toggle_item(word: str, avail: bool, lang: str):
    w = norm(word)
    m = ALIAS_MAP.get(w)
    if not m:
        for alias, cand in ALIAS_MAP.items():
            if w in alias or alias in w:
                m = cand
                break
    if not m:
        return T[lang]["not_on_menu"].format(word=word)
    m["available"] = avail
    key = "available_set" if avail else "finished_set"
    return T[lang][key].format(name=m["name"])


def confirm_text(sess: dict, lang: str) -> str:
    items = sess["pending"]
    total = bill_total(items)
    lines = bill_lines(items, lang)
    addr = sess.get("address")
    if addr:
        return T[lang]["bill"].format(lines=lines, total=f"{total:,}", addr=addr)
    return T[lang]["bill_no_addr"].format(lines=lines, total=f"{total:,}")


def finalize_order(phone: str, lang: str):
    global order_seq
    sess = get_session(phone)
    order_seq += 1
    oid = f"A-{order_seq}"
    items = sess["pending"]
    total = bill_total(items)
    addr = sess.get("address") or "—"
    now = datetime.now()
    ticket = {
        "id": oid, "phone": phone,
        "items": [{"name": m["name"], "qty": q, "price": m["price"]}
                  for m, q in items],
        "total": total, "address": addr, "status": "new",
        "time": now.strftime("%I:%M %p"), "date": now.date().isoformat(),
        "lang": lang,
    }
    tickets.append(ticket)
    customers.setdefault(phone, {"address": None, "orders": 0})
    customers[phone]["address"] = sess.get("address")
    customers[phone]["orders"] += 1
    sess["pending"] = []
    sess["state"] = "idle"
    sess["clarify_for"] = None
    return [tr(lang, "confirmed", oid=oid, eta=CFG.get("eta", "35-45"))]


def oversized_reply(items, phone: str, text: str, lang: str):
    """Route absurd quantities to a human instead of accepting them."""
    if any(q > MAX_QTY for _, q in items):
        notify_staff(f"BIG ORDER from {phone} (cap {MAX_QTY}/line): {text}")
        return T[lang]["big_order"]
    return None


def handle_message(phone: str, text: str):
    """Main entry point. Returns a list of reply strings."""
    lang = detect_lang(text)
    sess = get_session(phone)
    sess["lang"] = lang
    t = norm(text)
    replies = []

    # ---- owner commands (work even when the bot is disabled) ----
    if is_owner(phone):
        oc = owner_command(phone, text, lang)
        if oc:
            return oc
        if t in ("help", "commands"):
            return [T[lang]["owner_menu_hint"]]

    # ---- kill switch ----
    if not CFG.get("bot_enabled", True):
        return [T[lang]["bot_disabled"]]

    # ---- universal: cancel ----
    if t in NO:
        sess["pending"] = []
        sess["state"] = "idle"
        sess["clarify_for"] = None
        return [T[lang]["cancelled"]]

    # ---- state: awaiting confirmation ----
    if sess["state"] == "confirm":
        if t in YES:
            if sess.get("address"):
                return finalize_order(phone, lang)
            sess["state"] = "address"
            return [T[lang]["ask_address"]]
        items, clar, _unav, left = find_items(t)
        if items or clar:
            big = oversized_reply(items, phone, text, lang)
            if big:
                return [big]
            sess["pending"] = items
            if left and not sess.get("address"):
                sess["address"] = left
            if clar and not items:
                return start_clarify(sess, clar[0], lang)
            sess["state"] = "confirm"
            return [confirm_text(sess, lang)]
        if len(t) > 6:                       # probably an address
            sess["address"] = text.strip()
            return finalize_order(phone, lang)
        return [confirm_text(sess, lang)]

    # ---- state: awaiting clarification answer ----
    if sess["state"] == "clarify":
        key = sess.get("clarify_for")
        if key:
            name, options = CLARIFY[key]
            picked = None
            for oid_, label in options:
                if norm(label.split("(")[0]) in t or oid_.split("_")[-1] in t:
                    picked = oid_
                    break
            if picked:
                m = next(x for x in MENU if x["id"] == picked)
                sess["pending"].append((m, 1))
                sess["state"] = "confirm"
                sess["clarify_for"] = None
                return [confirm_text(sess, lang)]
            return [T[lang]["clarify"].format(
                name=name, options=" / ".join(l for _, l in options))]

    # ---- state: awaiting address ----
    if sess["state"] == "address":
        sess["address"] = text.strip()
        return finalize_order(phone, lang)

    # ---- idle: greetings / menu ----
    if t in GREET:
        return [tr(lang, "greeting")]
    if t in MENU_WORDS:
        return [menu_text(lang)]

    # ---- idle: complaints -> human ----
    if any(w in t for w in COMPLAINT_WORDS):
        notify_staff(f"ESCALATION from {phone}: {text}")
        return [T[lang]["handoff"]]

    # ---- idle: parse an order ----
    items, clar, unavailable, leftover = find_items(t)
    if not items and not clar:
        llm = llm_parse(t, lang)
        if llm:
            items, clar, unavailable, leftover = llm
    if clar and not items:
        return start_clarify(sess, clar[0], lang)
    big = oversized_reply(items, phone, text, lang)
    if big:
        return [big]
    if items:
        sess["pending"] = items
        if leftover and not sess.get("address"):
            sess["address"] = leftover
        sess["state"] = "confirm"
        if clar:                             # e.g. "2 zinger and karahi"
            sess["state"] = "clarify"
            sess["clarify_for"] = clar[0]
            name, options = CLARIFY[clar[0]]
            return [confirm_text(sess, lang),
                    T[lang]["clarify"].format(
                        name=name, options=" / ".join(l for _, l in options))]
        return [confirm_text(sess, lang)]
    if unavailable:
        return [T[lang]["finished_now"].format(name=unavailable[0])]

    sug = unknown_suggestion(t, lang)
    if sug:
        return [sug]
    # last resort: LLM for chit-chat, else fallback
    return [T[lang]["unknown_none"]]


def start_clarify(sess: dict, key: str, lang: str):
    name, options = CLARIFY[key]
    sess["state"] = "clarify"
    sess["clarify_for"] = key
    return [T[lang]["clarify"].format(
        name=name, options=" / ".join(l for _, l in options))]


# --------------------------- Sending -------------------------------
outbox = []  # demo-mode log of everything "sent"


def notify_staff(text: str):
    if OWNER_PHONE and WA_TOKEN and WA_PHONE_ID and http:
        send_whatsapp(OWNER_PHONE, text)
    else:
        outbox.append({"to": "STAFF", "text": text, "at": time.time()})


def send_whatsapp(to: str, text: str) -> bool:
    """Send via Meta Cloud API when configured; otherwise log to outbox."""
    if WA_TOKEN and WA_PHONE_ID and http:
        try:
            r = http.post(
                f"https://graph.facebook.com/v21.0/{WA_PHONE_ID}/messages",
                headers={"Authorization": f"Bearer {WA_TOKEN}",
                         "Content-Type": "application/json"},
                json={"messaging_product": "whatsapp", "to": to,
                      "type": "text", "text": {"body": text}},
                timeout=10)
            ok = r.status_code < 300
            if ok:
                try:
                    mid = r.json().get("messages", [{}])[0].get("id", "?")
                except Exception:
                    mid = "?"
                print(f"[sent -> {to}: {mid}]", flush=True)
            else:
                print(f"[send failed {r.status_code}: {r.text[:200]}]",
                      flush=True)
            return ok
        except Exception as e:
            print(f"[send error {e}]", flush=True)
            return False
    outbox.append({"to": to, "text": text, "at": time.time()})
    print(f"[demo] Would send via WhatsApp to {to}: {text[:60]}", flush=True)
    return True


def set_ticket_status(oid: str, status: str):
    for tk in tickets:
        if tk["id"] == oid:
            tk["status"] = status
            lang = tk.get("lang", "en")
            key = {"preparing": "ticket_preparing",
                   "ready": "ticket_ready"}.get(status)
            if key and not tk["phone"].startswith("demo"):
                send_whatsapp(tk["phone"], T[lang][key].format(oid=oid))
            return tk
    return None

# --------------------------- Security --------------------------------
# Meta retries webhook deliveries; without dedup a retried message would
# create a duplicate ticket. Bounded in-memory for the MVP — move to a
# UNIQUE column in Postgres for production.
_seen_ids = set()
_seen_order = deque(maxlen=5000)


def wamid_seen(wid: str) -> bool:
    """True if this Meta message id was already processed."""
    if not wid:
        return False
    if wid in _seen_ids:
        return True
    if len(_seen_order) == _seen_order.maxlen:
        _seen_ids.discard(_seen_order[0])   # evict oldest before auto-drop
    _seen_order.append(wid)
    _seen_ids.add(wid)
    return False


def verify_signature(raw: bytes, header: str) -> bool:
    """Check Meta's X-Hub-Signature-256 against the raw request body.
    Fail-open (with a loud startup warning) when WA_APP_SECRET is unset,
    so local testing isn't blocked. Always set it for a live number."""
    if not WA_APP_SECRET:
        return True
    if not header.startswith("sha256="):
        return False
    mac = hmac.new(WA_APP_SECRET.encode("utf-8"), raw,
                   hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + mac, header)


# --------------------------- FastAPI app ---------------------------
app = FastAPI(title=f"{BRAND} Order Bot")


@app.get("/webhook")
def webhook_verify(hub_mode: str = "", hub_verify_token: str = "",
                   hub_challenge: str = ""):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)
    return PlainTextResponse("forbidden", status_code=403)


@app.post("/webhook")
async def webhook_receive(req: Request):
    raw = await req.body()
    if not verify_signature(raw, req.headers.get("x-hub-signature-256", "")):
        return JSONResponse({"ok": False}, status_code=403)
    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"ok": False})
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            val = change.get("value", {})
            for msg in val.get("messages", []):
                if msg.get("type") != "text":
                    continue
                if wamid_seen(msg.get("id") or ""):
                    continue                      # Meta retry — skip
                sender = msg.get("from", "")
                if sender == "__owner__":
                    continue                      # demo sentinel never via webhook
                text = msg.get("text", {}).get("body", "")
                for reply in handle_message(sender, text):
                    send_whatsapp(sender, reply)
    return JSONResponse({"ok": True})


@app.post("/api/sim")
async def api_sim(req: Request):
    """Demo-mode chat: powers the /demo phone simulator.
    Disabled unless DEMO_MODE=1 — never expose owner impersonation live."""
    if not DEMO_MODE:
        return JSONResponse({"ok": False}, status_code=404)
    body = await req.json()
    phone = str(body.get("phone", "demo-customer")).strip() or "demo-customer"
    if not phone.startswith("demo") and phone != "__owner__":
        phone = "demo-" + re.sub(r"\D", "", phone)
    text = str(body.get("text", ""))
    replies = handle_message(phone, text)
    return {"replies": replies}


@app.get("/api/tickets")
def api_tickets():
    return {"tickets": list(reversed(tickets))}


@app.post("/api/tickets/{oid}/status")
async def api_ticket_status(oid: str, req: Request):
    body = await req.json()
    tk = set_ticket_status(oid, body.get("status", ""))
    return {"ok": bool(tk), "ticket": tk}


@app.get("/api/outbox")
def api_outbox():
    return {"outbox": outbox[-20:]}


# --------------------------- Pages ---------------------------------
LANDING_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{BRAND}} Order Bot</title>
<style>body{font-family:system-ui;max-width:720px;margin:40px auto;padding:0 20px;line-height:1.6;color:#222}
a.btn{display:inline-block;background:#25D366;color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;margin:6px 8px 6px 0;font-weight:600}
code{background:#f2f2f2;padding:2px 6px;border-radius:4px}ol li{margin:8px 0}</style></head><body>
<h1>{{BRAND}} — WhatsApp Order Bot</h1>
<p>The customer WhatsApps the restaurant. The bot takes the order, the kitchen
gets a ticket, the customer gets status updates. Staff only touch exceptions.</p>
<a class="btn" href="/demo">Try the demo chat</a>
<a class="btn" href="/kitchen" style="background:#333">Kitchen screen</a>
<h2>Try it right now (no setup)</h2>
<ol>
<li>Open <a href="/demo"><b>/demo</b></a> — it's a simulated WhatsApp chat.</li>
<li>Type: <code>2 zinger, 1 coke, DHA phase 6</code></li>
<li>Reply <code>yes</code>, then send any address.</li>
<li>Open <a href="/kitchen"><b>/kitchen</b></a> in another tab — the ticket is there.
Tap <b>Preparing</b> then <b>Ready</b> and watch the chat update.</li>
<li>Toggle <b>owner mode</b> in the demo and type <code>zinger finished</code> —
then order a zinger as a customer and see what happens.</li>
</ol>
<h2>Connect real WhatsApp (live pilot)</h2>
<ol>
<li>Create a Meta developer app, add the <b>WhatsApp</b> product — you instantly
get a <b>free test number</b> (no business verification needed for testing).</li>
<li>Add your own mobile under <b>To</b> so the test number can message you.</li>
<li>Expose this server: <code>ngrok http 8000</code></li>
<li>In the dashboard set webhook to <code>https://&lt;your-url&gt;/webhook</code>,
verify token = your <code>WA_VERIFY_TOKEN</code>.</li>
<li><code>RESTAURANT_CONFIG=restaurants/&lt;slug&gt;.json WA_TOKEN=... WA_PHONE_ID=... WA_VERIFY_TOKEN=... python app.py</code></li>
<li>Message the test number from your phone. Tickets land at <code>/kitchen</code>.</li>
</ol>
<p style="color:#666">Engine notes: sessions/orders live in memory (restart clears
them). For a pilot, move <code>sessions/customers/tickets</code> to Postgres.
Each restaurant is one JSON config — see <code>restaurants/</code>.</p>
</body></html>"""

KITCHEN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kitchen — {{BRAND}}</title>
<style>body{font-family:system-ui;background:#111;color:#eee;margin:0;padding:16px}
h1{font-size:20px;margin:4px 0 12px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.card{background:#1e1e1e;border-radius:10px;padding:14px;border-left:6px solid #25D366}
.card.preparing{border-color:#f5a623}.card.ready{border-color:#888;opacity:.55}
.card h3{margin:0 0 4px;font-size:18px}.meta{color:#aaa;font-size:13px;margin-bottom:8px}
.items{font-size:15px;margin:8px 0}.items div{padding:3px 0;border-bottom:1px solid #2c2c2c}
.total{font-weight:700;font-size:17px;margin-top:8px}
button{border:0;border-radius:6px;padding:10px 14px;font-size:15px;font-weight:600;margin:6px 6px 0 0;cursor:pointer}
.prep{background:#f5a623}.rdy{background:#25D366;color:#fff}
.addr{background:#262626;border-radius:6px;padding:8px;font-size:14px;margin-top:8px}</style></head><body>
<h1>{{BRAND}} — Kitchen Tickets <span id="c" style="color:#888;font-size:14px"></span></h1>
<div class="grid" id="g"></div>
<script>
async function load(){
  const r = await fetch('/api/tickets'); const d = await r.json();
  document.getElementById('c').textContent = d.tickets.length + ' total';
  const g = document.getElementById('g'); g.innerHTML = '';
  d.tickets.forEach(t=>{
    const el = document.createElement('div');
    el.className = 'card ' + t.status;
    el.innerHTML = '<h3>#' + t.id + ' <span style="font-size:13px;color:#aaa">' + t.time + '</span></h3>'
      + '<div class="meta">' + t.status.toUpperCase() + ' · ' + t.phone + '</div>'
      + '<div class="items">' + t.items.map(i=>'<div>'+i.qty+'x '+i.name+'</div>').join('') + '</div>'
      + '<div class="total">Rs ' + t.total.toLocaleString() + '</div>'
      + '<div class="addr">' + t.address + '</div>';
    if(t.status==='new'){ const b=document.createElement('button'); b.className='prep'; b.textContent='Preparing';
      b.onclick=()=>set(t.id,'preparing'); el.appendChild(b); }
    if(t.status==='preparing'){ const b=document.createElement('button'); b.className='rdy'; b.textContent='Ready → rider';
      b.onclick=()=>set(t.id,'ready'); el.appendChild(b); }
    g.appendChild(el);
  });
}
async function set(id,s){ await fetch('/api/tickets/'+id+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:s})}); load(); }
load(); setInterval(load, 2500);
</script></body></html>"""

DEMO_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Demo — {{BRAND}} WhatsApp</title>
<style>body{font-family:system-ui;background:#0b141a;margin:0;display:flex;justify-content:center;min-height:100vh}
.phone{width:400px;max-width:100vw;height:100vh;background:#0b141a;display:flex;flex-direction:column}
.hd{background:#075E54;color:#fff;padding:12px 16px;font-weight:600}
.hd small{display:block;font-weight:400;opacity:.8;font-size:12px}
.bar{background:#1f2c34;color:#ccc;padding:8px 12px;font-size:13px;display:flex;gap:10px;align-items:center}
.bar input{background:#2a3942;border:0;color:#fff;border-radius:6px;padding:6px 8px;width:130px}
.chat{flex:1;overflow-y:auto;padding:14px 10px;background:#0b141a url('') repeat}
.msg{max-width:78%;padding:8px 12px;border-radius:8px;margin:6px 0;font-size:15px;line-height:1.45;white-space:pre-wrap}
.me{background:#005c4b;color:#fff;margin-left:auto;border-top-right-radius:0}
.bot{background:#1f2c34;color:#e7e7e7;border-top-left-radius:0}
.in{display:flex;padding:10px;background:#1f2c34;gap:8px}
.in input{flex:1;border:0;border-radius:20px;padding:12px 16px;font-size:15px;background:#2a3942;color:#fff}
.in button{border:0;background:#00a884;color:#fff;border-radius:50%;width:46px;height:46px;font-size:20px;cursor:pointer}
.hint{color:#8696a0;font-size:12px;padding:6px 14px}</style></head><body>
<div class="phone">
<div class="hd">{{BRAND}}<small>online · replies instantly</small></div>
<div class="bar"><label><input type="checkbox" id="own"> owner mode</label>
<input id="ph" value="03001234567" title="customer number"></div>
<div class="hint">Try: <b>2 zinger, 1 coke, DHA phase 6</b> → <b>yes</b> → send an address. Or <b>menu</b>, or <b>1 karahi</b>.</div>
<div class="chat" id="c"></div>
<div class="in"><input id="t" placeholder="Type a message" autocomplete="off"><button id="s">➤</button></div>
</div>
<script>
const c=document.getElementById('c'), t=document.getElementById('t');
function add(txt,cls){const d=document.createElement('div');d.className='msg '+cls;d.textContent=txt;c.appendChild(d);c.scrollTop=c.scrollHeight;}
async function send(){
  const v=t.value.trim(); if(!v) return; t.value=''; add(v,'me');
  const owner=document.getElementById('own').checked;
  const phone=owner?'__owner__':document.getElementById('ph').value;
  const r=await fetch('/api/sim',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone,text:v})});
  const d=await r.json(); (d.replies||[]).forEach(x=>add(x,'bot'));
}
document.getElementById('s').onclick=send;
t.addEventListener('keydown',e=>{if(e.key==='Enter')send();});
add("Assalam-o-Alaikum! {{BRAND}} mein khush aamdeed. Apna order bhejein — misal '2 zinger, 1 coke'.",'bot');
</script></body></html>"""


def brand_page(page_html: str) -> str:
    return page_html.replace("{{BRAND}}", html.escape(BRAND))


@app.get("/", response_class=HTMLResponse)
def page_home():
    return brand_page(LANDING_HTML)


@app.get("/kitchen", response_class=HTMLResponse)
def page_kitchen():
    return brand_page(KITCHEN_HTML)


@app.get("/demo", response_class=HTMLResponse)
def page_demo():
    if not DEMO_MODE:
        return HTMLResponse("Not found", status_code=404)
    return brand_page(DEMO_HTML)


# --------------------------- Scaffold ------------------------------
def _ask(prompt: str, default: str = "") -> str:
    extra = f" [{default}]" if default else ""
    s = input(f"{prompt}{extra}: ").strip()
    return s or default


def _ask_int(prompt: str) -> int:
    while True:
        s = input(f"{prompt}: ").strip().replace(",", "")
        if s.isdigit() and int(s) > 0:
            return int(s)
        print("  Enter a positive whole number.")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "restaurant"


def _item_id(name: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "item"
    iid, n = base, 2
    while iid in taken:
        iid = f"{base}_{n}"
        n += 1
    return iid


def scaffold():
    """Interactive setup: build a restaurant config in ~10 minutes."""
    print("=== FoodHub — new restaurant setup ===\n")
    name = _ask("Restaurant name") or "My Restaurant"
    slug = _ask("Short slug (used for the file name)", _slug(name))
    owner = _ask("Owner WhatsApp number, digits only, no + (blank skips)")
    eta = _ask("Delivery ETA text shown to customers", "35-45")
    menu, clarify, taken = [], [], set()
    print("\n--- Menu items (blank name = done) ---")
    while True:
        iname = input("\nItem name: ").strip()
        if not iname:
            break
        if _ask("  Has variants like half/full? (y/N)", "n").lower() == "y":
            key = _item_id(iname, taken)
            key_word = iname.lower().split()[-1]
            variants = []
            for label in ("Half", "Full"):
                price = _ask_int(f"  Price Rs for '{iname}' {label}")
                vid = _item_id(f"{iname} {label}", taken)
                taken.add(vid)
                vname = f"{iname} {label}"
                valiases = list(dict.fromkeys([
                    vname.lower(),
                    f"{iname.lower()} {label.lower()}",
                    f"{label.lower()} {iname.lower()}",
                ]))
                menu.append({"id": vid, "name": vname, "price": price,
                             "aliases": valiases, "available": True})
                variants.append({"id": vid, "label": f"{label} (Rs {price:,})"})
            caliases = [iname.lower()]
            if len(key_word) > 2 and key_word != iname.lower():
                caliases.append(key_word)
            clarify.append({"key": key, "name": iname,
                            "aliases": caliases, "options": variants})
            print(f"  + {iname} (Half/Full) — bot will ask which one")
        else:
            price = _ask_int(f"  Price Rs for '{iname}'")
            iid = _item_id(iname, taken)
            taken.add(iid)
            extra = _ask("  Extra aliases, comma-separated (blank = auto)")
            last = iname.lower().split()[-1]
            auto = [iname.lower()]
            if len(last) > 2 and last != iname.lower():
                auto.append(last)   # e.g. "karahi" for "Chicken Karahi"
            aliases = list(dict.fromkeys(
                auto + [a.strip().lower() for a in extra.split(",") if a.strip()]))
            menu.append({"id": iid, "name": iname, "price": price,
                         "aliases": aliases, "available": True})
            print(f"  + {iname} — Rs {price:,}")
    cfg = {"name": name, "slug": slug, "owner_phone": owner,
           "currency": "Rs", "eta": eta, "menu": menu, "clarify": clarify}
    errs = validate_config(cfg)
    if errs:
        print("\nConfig has problems:")
        for e in errs:
            print("  -", e)
        if _ask("Save anyway? (y/N)", "n").lower() != "y":
            print("Discarded.")
            return
    path = os.path.join(BASE_DIR, "restaurants", f"{slug}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"\nSaved → {path}  ({len(menu)} items)")
    print("\nNext steps:")
    print(f"  RESTAURANT_CONFIG={path} \\")
    print("    WA_TOKEN=... WA_PHONE_ID=... WA_VERIFY_TOKEN=... python app.py")
    if owner:
        print(f"  (owner number {owner} is stored in the config — "
              f"no OWNER_PHONE env needed)")
    print("  Then open /demo to test and /kitchen for the ticket screen.")


def check(path: str):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    errs = validate_config(cfg)
    if errs:
        print(f"{path}: INVALID")
        for e in errs:
            print("  -", e)
        sys.exit(1)
    print(f"{path}: OK "
          f"({len(cfg['menu'])} items, {len(cfg.get('clarify', []))} clarifications)")


if __name__ == "__main__":
    if "--new" in sys.argv:
        scaffold()
        sys.exit(0)
    if "--check" in sys.argv:
        i = sys.argv.index("--check")
        check(sys.argv[i + 1] if i + 1 < len(sys.argv) else CONFIG_PATH)
        sys.exit(0)
    import uvicorn
    print(f"{BRAND} order bot running → http://localhost:{PORT}")
    print(f"  config      : {CONFIG_PATH}")
    print(f"  demo chat   : http://localhost:{PORT}/demo")
    print(f"  kitchen     : http://localhost:{PORT}/kitchen")
    if not WA_APP_SECRET:
        print("  WARNING: WA_APP_SECRET not set — webhook signatures NOT "
              "verified. Set it before connecting a live number.")
    if DEMO_MODE:
        print("  demo mode ON (/demo + /api/sim enabled). "
              "Set DEMO_MODE=0 for production.")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
