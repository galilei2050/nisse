# Browser actions — acting on logged-in web pages

How nisse goes from *reading* the web to *doing* things on it (open a page behind a login, read it,
click, type, scroll), and the decisions behind the design. Code: `app/browser/`, `app/startbrowser.py`,
and baski's `PlaywrightClient`.

> **Состояние на 2026-08-09: в дереве лежат только инструменты, и они ни к чему не подключены.**
> Перенесено из ветки #34 (исследование и замеры ниже — от 2026-06-27): `tools.py`, `session.py`,
> `store.py`, `proxy.py` и их тесты. Пакет не регистрируется в реестре (`app/tools/`), поэтому ни
> один агент этих имён не видит — это сделано намеренно, чтобы разобрать перенос до подключения.
>
> Ниже описан замысел целиком, и часть его в дереве отсутствует. Чего нет: управляемого удалённого
> браузера (`managed.py`, Browserbase через CDP — без него на сайтах под Cloudflare можно читать, но
> не покупать), `make startbrowser` (это он ЗАПИСЫВАЕТ сессию чата, так что `BrowserSessionStore.load`
> пока всегда возвращает None), обвязки в `CoreDeps`/`Conversations`, и сценария оплаты. Читать как
> обоснование и как список того, что понадобится при подключении, а не как опись файлов.

## Why this exists, and what it is not

baski already had a read-only fetcher (`WebBrowseTool` → page-to-markdown). It can't reach anything
behind a login and can't interact. Browser actions add a **per-chat, logged-in browser session** the
agent reads as an **indexed element listing** and acts on by element `ref`.

It is deliberately **not** a vision/screenshot agent — text tokens are far cheaper than screenshots.
(Industry consensus from the competitor sweep: browser-use defaults `use_vision=False`; Playwright-MCP
drives off structured data, not pixels.)

### Snapshot model — indexed elements, not raw `aria_snapshot` (the research pivot)

The first cut read pages with Playwright's `aria_snapshot` and clicked by `get_by_role(role, name=…)`.
On a real e-commerce SPA (DoorDash grocery) that **broke three ways**, and a best-practices sweep
(browser-use, Stagehand, Playwright-MCP) pointed at the same fix each time:

- **Huge, noisy tree** (600+ nodes of nav/categories) floods context → emit only a **compact list of
  interactive elements**.
- **Identical accessible names** (every product's add button is "0 in cart, click to edit quantity")
  make role+name ambiguous → tag each element with a synthetic numeric **`ref`** and act by ref.
- **Prices live in non-ARIA text**, absent from the accessibility name → **merge DOM**: for each
  element, pull the nearest ancestor's visible text (the product card), which carries the price.

So `_snapshot` injects a small JS pass (`_INDEX_JS` in `session.py`) that tags every visible
interactive element with `data-nisse-ref` and returns `[ref] role "label" — nearby text`. The agent
reads prices, picks by ref; the executor clicks `[data-nisse-ref='N']`. Refs are valid only for the
most recent listing (re-tagged every action). This is the browser-use / Stagehand "action graph"
pattern: deterministic DOM extraction, LLM reasons over a compact representation.

### Bot protection — three layers, and what each needs

DoorDash fronts everything with Cloudflare. Reading the page is solvable locally; *transacting*
(cart/checkout) is not — that's why the agent runs on a managed remote browser (below).

- **Managed-challenge interstitial** ("Just a moment…") on some navigations. A session carrying a
  valid clearance cookie auto-solves it in seconds, but a quick snapshot catches it mid-solve. Fix:
  `_settled_snapshot` detects the interstitial by title and **waits for it to clear** (~18s) instead
  of returning the challenge page. Do **not** reload — that restarts it.
- **Invisible Turnstile click-overlay** (`data-testid="turnstile/overlay"`) that intercepts pointer
  events so normal clicks never reach the button. Fix: `click(force=True)` dispatches straight to the
  element, past the overlay. Lazy-loaded grids need **`web_scroll`** to render first.
- **Server-side Turnstile token enforcement** — the real wall. Even with the overlay bypassed, a
  *local* headless/automated browser's cart-write requests are silently rejected (cart stays 0); the
  add-to-cart API wants a valid Turnstile token an automated local browser can't mint. Verified
  directly: force-click adds nothing locally, but the *same* click on a **managed remote browser
  (Browserbase)** takes the cart 0→1→2. Conclusion below.

### The fix: a managed remote browser over CDP

`PlaywrightClient(cdp_url=…)` attaches to a managed/fortified browser via `connect_over_cdp` instead
of launching local Chromium. We use **Browserbase**: `app/browser/managed.py` creates a session and
returns its CDP URL; baski connects, reuses the remote browser's single context, and merges our saved
cookies in with `add_cookies`. Browserbase's browser is trusted by Turnstile, so cart/checkout writes
stick. Enabled by `BROWSERBASE_API_KEY` + `BROWSERBASE_PROJECT_ID`; unset → local Chromium (fine for
dev and unprotected sites). Cross-checked vs Bright Data (more enterprise, more signup friction);
Browserbase chosen for lower friction and it cleared DoorDash on the free tier. In managed mode the
proxy pool is skipped — Browserbase provides its own egress.

## The session model — one cookie jar per chat

Every other nisse store is scoped per `conversation_id`; the browser session is too. The login a
chat established must not leak into another chat. Concretely:

- A **shared** `PlaywrightClient` (one Chromium per process) launches the browser. Its default
  context is **anonymous** and serves the read-only `browse_website` fetch.
- Each chat gets its **own isolated context** branched off that browser via
  `PlaywrightClient.new_context(...)`, loaded with that chat's saved session. That's the cookie jar
  for the chat's logged-in actions. See `BrowserSession` (`app/browser/session.py`) and
  `Conversations._build_browser_action_tools`.

Session state (a Playwright **storage-state**: cookies + localStorage) is stored per chat in
**MongoDB** (`browser_sessions`, one doc per `conversation_id`, via `BrowserSessionStore` in
`app/browser/store.py`) — the same per-conversation store pattern as lists/memories/prompts. It's
small per-user data read on each action, so it belongs in Mongo, not object storage (GCS is for large
write-once blobs like traces). Mongo is also what makes the session reach **production**: Cloud Run is
stateless, so a local file wouldn't survive — the agent reads the session from Mongo on every boot.

### Why Chromium, not Firefox

The original client was Firefox. Acting on a logged-in session needs a clean way to *capture* that
session once and *reuse* it. Research verdict (two independent passes, against Playwright's own
docs): **Chromium has a clean session-reuse story** (storage_state round-trips, shared profile, CDP
attach all work); **Firefox has no supported path** — storage_state can't be imported into a real
browser, `connectOverCDP` is Chromium-only, and Playwright ships a patched Firefox that can't share
the branded profile. So baski's `PlaywrightClient` now launches Chromium. It's nisse's only consumer
(clarity has its own copy), so the swap had no other blast radius.

### Capturing the session: `make startbrowser`

`make startbrowser U=<chat-id>` (→ `app/startbrowser.py`) launches the **real Chromium binary
directly** (not via `playwright.launch`, whose automation flags Cloudflare detects) on a persistent
on-disk profile, you log into the services you care about — and **save your payment card on those
sites** — then press Enter; the session is captured over CDP and **saved to that chat's MongoDB
document**. The first page is `about:blank` by default; pass `URL=<site>` to open one directly (e.g.
`make startbrowser U=42 URL=https://www.doordash.com`), or just navigate there once the browser is up.
Re-run when a login expires (see "Honest limits").

The same window is the answer to "I want to see a browser where I can log in": you *are* logged in
there; the file just hands that session to the agent. There is no need (and no clean way) to push the
agent's cookies back into your personal Firefox.

## Payments — autonomous, capped at the bank

Decision (owner): the agent may **pay autonomously**; the blast radius is capped at the bank, not by
a human gate. The mechanism keeps nisse entirely out of card-handling:

- A dedicated **Revolut standard virtual card** (stable number, with a per-transaction / monthly
  limit) is **saved on the merchant sites** during `make startbrowser`.
- At checkout the agent selects the already-saved card and confirms. **nisse never sees, stores, or
  types a card number** — no PAN in the model context or traces, no vault, no PCI surface, no
  payment-iframe problem (that wall only bites when you *type* a card).
- The Revolut limit + freeze is the safety net: worst case (a bug or prompt-injection) loses at most
  the limit. Use a **standard** virtual card, not a disposable one — the saved-card flow needs a
  stable number.

Ceiling to be aware of: autonomous pay works on sites with a **saved card / one-click**; a generic
PSP checkout that requires typing the card into a cross-origin iframe will block automated entry —
that's expected, the agent reports it and you finish manually.

## Residential proxies — sticky per site

The owner has ~10 residential proxies. `ProxyPool` (`app/browser/proxy.py`) **pins one proxy per
host** and keeps it until that proxy is marked banned on that host, then rotates to the
least-loaded non-banned one. Sticky assignment minimises the "new IP" churn that trips bot-detection
and session checks (the precedent is profit's cloud-egress proxy). The pinned proxy is applied at
context creation (`new_context(proxy=...)`).

Proxies load from the **`BROWSER_PROXIES`** env var — the `host:port:username:password` lines a
Webshare "download list" URL returns. Pasting the lines into `.env` keeps the **provider API token
out of this app** (you curl the URL once yourself). Unset → direct connection.

`mark_banned(url)` is the rotation hook; reliable *automatic* ban detection (403 / CAPTCHA / block
page) is not solved here — sticky assignment + a manual/heuristic rotate is the current scope.

## The tools the agent sees

In `app/browser/tools.py`, sharing one `BrowserSession` per chat — each returns the post-action
indexed listing so the agent always works from the current page:

- `web_open(url)` — navigate the logged-in session, return the indexed listing.
- `web_snapshot()` — re-read the current page as a fresh listing (re-tags refs).
- `web_click(ref)` — click the element with this `[ref]` (force-click, past the Turnstile overlay).
- `web_type(ref, text, submit)` — type into the field with this `[ref]`; `submit` presses Enter.
- `web_scroll()` — scroll down to render lazy-loaded content (product grids), then re-read.

These are distinct from `browse_website` (read-only public fetch → markdown). Guidance baked into the
descriptions: prefer the **search tools** for finding facts/URLs; reach for the browser only when you
must read behind a login or interact. Action failures return the reason + a prompt to re-snapshot, so
the agent self-corrects rather than crashing the turn.

## Action use-cases (ranked by value × feasibility, single owner)

1. **Logged-in reads** — order history, balances, "did it ship?" — behind a login the public fetcher
   can't reach. Highest ROI, lowest risk; the first thing to lean on.
2. **Reorder / buy on a saved-card site** — enabled by the Revolut saved card.
3. **Booking** — restaurants / appointments / tickets without an API.
4. **Form-filling** — registrations, applications (the agent already has the owner's facts in core
   memory).
5. **Account / subscription management** — cancel, change plan, fetch invoices.
6. **Research-and-act** — compare across sites (via search), then act (via the browser).

## North-star acceptance task

"Buy cucumbers at the nearest Safeway and order via DoorDash." This is the target the capability is
built toward, not a solved one-shot: DoorDash is a JS-heavy, bot-protected, logged-in checkout. It
needs (a) a `make startbrowser` session logged into DoorDash with the Revolut card saved, (b) a
residential proxy pinned to the host, and (c) iteration on the open→snapshot→click/type loop. The
primitives are verified end-to-end on real sites (`scratch/e2e_browser.py`: read, type+submit, click);
the DoorDash flow is the next milestone to drive and harden.

## Honest limits

- **Sessions expire** (TTL, device-binding, 2FA "new device"). Inevitable; recovery is re-running
  `make startbrowser`. Hardened providers (Google/Microsoft/banks) may refuse a session moved to a
  cloud IP regardless — keep those flows on the owner's machine or expect re-auth.
- **One context per chat lives for the process lifetime** (closed when the browser closes). Fine for
  a single owner; revisit if many chats act concurrently.
- **Autonomous pay** is bounded by the saved-card requirement and the Revolut limit (above).

## Configuration

- `BROWSERBASE_API_KEY` + `BROWSERBASE_PROJECT_ID` (optional) — set both to route the browser through
  Browserbase's managed browser (required to transact on Cloudflare-protected sites like DoorDash, and
  the only autonomous option in prod since Cloud Run has no display). Unset → local Chromium.
- `BROWSER_PROXIES` — `host:port:user:pass` lines (the Webshare "download list" output; the provider
  API token stays out of the app). Required in **local** mode; unused in managed mode (Browserbase
  provides egress).
- Per-chat sessions live in MongoDB (`browser_sessions`), so there's **no** session-dir env var; the
  same `MONGODB_URI` the rest of the app uses covers it.
- Browsers: `playwright install chromium` (Dockerfile installs it `--with-deps`); managed mode needs
  the `browserbase` SDK (a project dependency).
