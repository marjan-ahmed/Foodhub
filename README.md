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

1. **Collect the PKR 35k setup fee upfront.** (For the very first pilot
   client, consider waiving it until the 2-week pilot converts — the prize
   there is proof and a testimonial, not the fee.)
2. **Scaffold the config** — ~10 minutes, guided prompts:
   ```bash
   python app.py --new
   ```
   Validates as it goes (duplicate items, bad prices, dangling variant ids).
   Re-check any config later with:
   ```bash
   python app.py --check restaurants/<slug>.json
   ```
3. **Get a spare SIM from the owner.** The owner buys a new SIM (~Rs 150–350,
   biometric verification at any franchise — you can't do this part for
   them). It must never have been on WhatsApp before. Register it as the
   bot's number in the Meta dashboard (Step 2: Production setup, OTP via
   call/SMS — the owner just reads you the code). The number becomes
   API-only — it can't stay in the WhatsApp app, so **never use the owner's
   daily number**.
4. **Deploy their instance.** Fill in `.env` (copy from `.env.example`):
   ```bash
   python app.py
   ```
   Point the Meta webhook at `https://<your-url>/webhook` — the `/webhook`
   path is required; the bare domain will fail verification. Subscribe to
   the `messages` field.
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

Copy `.env.example` to `.env` and fill it in — **the app loads `.env`
automatically on startup** (a real environment variable always overrides the
file, so per-terminal `set`/`export` still works when you need it). Never
commit the real `.env` (it's gitignored).

| Var | Needed for | Notes |
|---|---|---|
| `RESTAURANT_CONFIG` | always | path to the restaurant JSON (default: demo) |
| `WA_TOKEN` | live WhatsApp | Meta dashboard → permanent via System Users |
| `WA_PHONE_ID` | live WhatsApp | phone_number_id from the dashboard. This is the *default* sending number; when several numbers share one app instance, each reply goes out via whichever number received the message (see "Multi-number behavior") |
| `WA_VERIFY_TOKEN` | live WhatsApp | you invent it; must match dashboard |
| `WA_APP_SECRET` | live WhatsApp | Meta App secret; verifies webhook signatures (required live) |
| `DEMO_MODE` | safety | `1` (default) enables /demo + /api/sim; set `0` in production |
| `MAX_QTY` | safety | per-line quantity cap before human review (default 20) |
| `OWNER_PHONE` | fallback | only if config has no `owner_phone` |
| `LLM_API_KEY` | optional | fallback for messages the parser can't handle |
| `LLM_BASE_URL` | optional | OpenAI-compatible endpoint. For a free Gemini key (aistudio.google.com): `https://generativelanguage.googleapis.com/v1beta/openai/` |
| `LLM_MODEL` | optional | e.g. `gemini-2.5-flash` for the Gemini endpoint, `gpt-4o-mini` for OpenAI |

Owner commands over WhatsApp: `MENU` · `'<item> finished'` · `'<item> available'`
· `TODAY` · `'bot off'` / `'bot on'` (kill switch — customers get a "we're
closed" reply while off).

## Live demo over real WhatsApp (ngrok)

For a restaurant visit, a bot answering on the owner's own phone beats any
screen demo. On your laptop:

```bash
python app.py            # with live credentials in .env, DEMO_MODE=0
ngrok http 8000          # second terminal
```

Set the Meta webhook callback to `https://<ngrok-url>/webhook`, verify, and
subscribe to `messages`. Then have the owner message the test/bot number from
their phone.

**ngrok free rotates the URL on every restart** — each restart means updating
the dashboard callback and re-verifying. For a demo: update once, then don't
touch ngrok, don't close the laptop, and disable sleep. For a real pilot, use
a stable host (VPS / Railway / Render) or paid ngrok — the rotating URL is
too fragile for production.

## Multi-number behavior

One app instance can serve several WhatsApp numbers (all subscribed to the
same webhook). Each reply goes out via **whichever number received the
message** (read from the webhook's `phone_number_id`; `WA_PHONE_ID` is only
the fallback), and customer sessions are **scoped per number** — the same
person ordering on two numbers gets two separate carts. Kitchen tickets
record which number took the order (`via`).

## Troubleshooting

- **Webhook verifies (200) but message events never arrive.** The `messages`
  subscription is stale — dashboard → Webhooks → unsubscribe `messages`,
  subscribe again, then hit **Test** and watch the ngrok inspector
  (`http://127.0.0.1:4040`) for the POST.
- **Bot receives but the phone gets no reply.** Check the terminal: `[sent
  -> <number>: <wamid>]` means Meta accepted the reply; `[send failed
  <code>: ...]` names the exact cause (dead token, wrong phone ID…);
  `[demo] Would send via WhatsApp …` means the running app has no
  `WA_TOKEN` (usually: code updated but `git pull` + restart forgotten).
  Unsent replies pile up visibly at `/api/outbox`.
- **Callback URL rejected at verification.** It must end in `/webhook`;
  the app must be running with the matching `WA_VERIFY_TOKEN` when you
  click Verify.
- **Meta 2FA SMS never arrives (common in Pakistan).** Use the voice-call
  option under "Need another way to authenticate?", and add an
  authenticator app as the 2FA method (Facebook → Settings → Accounts
  Center → Password and security) so it never blocks you again.
- **Dashboard shows "This phone number is already registered".** Migrating a
  number into the API permanently disconnects it from the WhatsApp app.
  Never migrate anyone's daily number — always a fresh spare SIM.

## Costs to know (Pakistan — verified Oct 2026)

Meta's Pakistan rate card, effective **1 October 2026**:

| Category | USD / delivered message | ≈ PKR |
|---|---|---|
| Marketing | $0.0473 | ~13.1 |
| Utility / Authentication / **Service** | **$0.0150** | **~4.15** |

Since 1 Oct 2026, **service replies inside the 24-hour customer window are
billed** (they were free before). Each business number gets the **first 1,000
service messages/month free**; beyond that they're billed at the utility rate.
Utility templates sent inside the window are billed from the first message.
Sources: Meta's published rate cards via
[engagelab.com](https://www.engagelab.com/blog/whatsapp-business-api-pricing),
[tekkpak.com](https://tekkpak.com/chatbot-companies-in-pakistan/),
[wab2c.com](https://wab2c.com/resources/blog/whatsapp-business-api-pricing-pakistan-2026).
Re-check [Meta's pricing page](https://developers.facebook.com/docs/whatsapp/pricing)
before quoting a client — Meta changes these rules.

Worked example: 50 orders/day × ~5 bot messages × 30 days = ~7,500 messages.
Minus 1,000 free = ~6,500 × $0.015 ≈ **$97 ≈ PKR 27,000/month** in Meta fees.
That's why the model is **PKR 35k setup + PKR 20k/month retainer, Meta fees
passed through at cost** — a flat monthly fee would lose money on Meta's bill
alone at real volumes. Sales tactic: offer the first two weeks as a free pilot
(you absorb the small pilot Meta bill) so the owner sees it work before paying.

## Repo layout

```
app.py                 # the engine (single file): bot + API + pages + scaffold
restaurants/           # one JSON config per restaurant
  demo.json            # sample config used by default
requirements.txt
.env.example
```

## Roadmap (not built yet)

Postgres for sessions/tickets/wamid-log (currently in-memory), multi-tenant
single process (currently one process per restaurant), per-restaurant Meta cost
tracking. Already in: webhook signature verification, wamid dedup, quantity
cap, demo-mode gating, kill switch, `.env` auto-loading, send-result logging,
and multi-number reply routing with per-number sessions.

## Security notes for a live number

- Set `WA_APP_SECRET` and `DEMO_MODE=0` before connecting a real number.
- The kill switch (`'bot off'` from the owner number) is in-memory; a restart
  re-enables the bot. Move it into the restaurant config/DB for production.
- When `LLM_API_KEY` is set, raw customer text is sent to the configured LLM
  endpoint. Decide on customer disclosure before enabling it for a client.
