# FoodHub — WhatsApp Restaurant Order Bot

One engine, many restaurants. A customer WhatsApps the restaurant's number —
the bot takes the order (English / Roman Urdu), confirms the bill, sends a
ticket to the kitchen screen, and updates the customer as the chef taps
**Preparing → Ready**. Staff only touch the weird cases.

Each restaurant is a JSON config (`restaurants/<slug>.json`). Onboarding a new
client is data entry, not engineering.

## Quickstart (no setup, 2 minutes)

```bash
pip install -r requirements.txt
python app.py
```

- Demo chat: http://localhost:8000/demo
- Kitchen screen: http://localhost:8000/kitchen

Try in the demo: `2 zinger, 1 coke, DHA phase 6` → `yes` → send an address.
Then open `/kitchen` in another tab, tap **Preparing** → **Ready**, and watch
the chat update. Toggle **owner mode** and type `zinger finished`.

## Onboarding a new restaurant (the 48-hour playbook)

1. **Collect the PKR 35k setup fee upfront.**
2. **Scaffold the config** — ~10 minutes, guided prompts:
   ```bash
   python app.py --new
   ```
   Validates as it goes (duplicate items, bad prices, dangling variant ids).
   Re-check any config later with:
   ```bash
   python app.py --check restaurants/<slug>.json
   ```
3. **Get a spare SIM from the owner.** Register it as the bot's WhatsApp number
   in the Meta dashboard (Step 2: Production setup, OTP via call/SMS). The
   number becomes API-only — it can't stay in the WhatsApp app, so never use
   the owner's daily number.
4. **Deploy their instance:**
   ```bash
   RESTAURANT_CONFIG=restaurants/<slug>.json \
   WA_TOKEN=... WA_PHONE_ID=... WA_VERIFY_TOKEN=... \
   python app.py
   ```
   Point the Meta webhook at `https://<your-url>/webhook`.
5. **Kitchen screen** — a cheap tablet at the counter on `/kitchen`.
6. **Test together** — order from your phone while the owner watches the
   ticket appear. Chef taps the buttons, you get the "ready" message.
7. **Go live** — bot number + QR code on the menu, flyers, Google profile.

## Config format

`restaurants/<slug>.json`:

```json
{
  "name": "Karachi Foods",
  "slug": "karachi-foods",
  "owner_phone": "923001234567",
  "eta": "35-45",
  "menu": [
    {"id": "zinger", "name": "Zinger Burger", "price": 550,
     "aliases": ["zinger", "zinger burger"], "available": true}
  ],
  "clarify": [
    {"key": "karahi", "name": "Chicken Karahi", "aliases": ["karahi"],
     "options": [
       {"id": "karahi_half", "label": "Half (Rs 900)"},
       {"id": "karahi_full", "label": "Full (Rs 1,650)"}
     ]}
  ]
}
```

- `aliases` is where Roman-Urdu tolerance and misspellings live.
- `clarify` entries make the bot ask a follow-up ("Half or Full?") before
  the item can be ordered.
- `owner_phone` enables owner commands over WhatsApp (`<item> finished`,
  `<item> available`, `TODAY`).

## Environment variables

| Var | Needed for | Notes |
|---|---|---|
| `RESTAURANT_CONFIG` | always | path to the restaurant JSON (default: demo) |
| `WA_TOKEN` | live WhatsApp | Meta dashboard → permanent via System Users |
| `WA_PHONE_ID` | live WhatsApp | phone_number_id from the dashboard |
| `WA_VERIFY_TOKEN` | live WhatsApp | you invent it; must match dashboard |
| `OWNER_PHONE` | fallback | only if config has no `owner_phone` |
| `LLM_API_KEY` | optional | fallback for messages the parser can't handle; free tier at aistudio.google.com |

## Costs to know (Pakistan, verify before quoting a client)

Meta charges ~$0.015/message for Pakistan. Replies inside the 24-hour
customer-service window are free up to 1,000/month per number — the normal
order flow lives inside that window. Business-initiated template messages
outside it are billed. Pricing model: PKR 35k setup + PKR 20k/month retainer,
Meta fees passed through at cost.

## Repo layout

```
app.py                 # the engine (single file): bot + API + pages + scaffold
restaurants/           # one JSON config per restaurant
  demo.json            # sample config used by default
requirements.txt
.env.example
```

## Roadmap (not built yet)

Postgres for sessions/tickets (currently in-memory), multi-tenant single
process (currently one process per restaurant), production webhook signature
verification, per-restaurant Meta cost tracking.
