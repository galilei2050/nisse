# Browser actions — acting on logged-in web pages

How nisse goes from *reading* the web to *doing* things on it (open a page behind a login, read it,
click, type), and the decisions behind the design. Code: `app/browser/`, `app/startbrowser.py`,
`app/shared/browser.py`, and baski's `PlaywrightClient`.

## Why this exists, and what it is not

baski already had a read-only fetcher (`WebBrowseTool` → page-to-markdown). It can't reach anything
behind a login and can't interact. Browser actions add a **per-chat, logged-in browser session** the
agent reads as an accessibility tree and acts on by role + name.

It is deliberately **not** a vision/screenshot agent. Pages are read via Playwright's
`aria_snapshot` (roles + accessible names, compact text), and elements are targeted with
`get_by_role(role, name=...)` — the same handles the snapshot exposes. Reasons: text tokens are far
cheaper than screenshots, and role/name targeting is more stable than pixel coordinates. (Industry
consensus from the competitor sweep: browser-use defaults `use_vision=False`; Playwright-MCP drives
off the accessibility tree, not pixels.)

## The session model — one cookie jar per chat

Every other nisse store is scoped per `conversation_id`; the browser session is too. The login a
chat established must not leak into another chat. Concretely:

- A **shared** `PlaywrightClient` (one Chromium per process) launches the browser. Its default
  context is **anonymous** and serves the read-only `browse_website` fetch.
- Each chat gets its **own isolated context** branched off that browser via
  `PlaywrightClient.new_context(...)`, loaded with that chat's saved session. That's the cookie jar
  for the chat's logged-in actions. See `BrowserSession` (`app/browser/session.py`) and
  `Conversations._build_browser_action_tools`.

Session state lives in a Playwright **storage-state** file (cookies + localStorage) keyed by chat,
under `browser_state_path(conversation_id)` (`app/shared/browser.py`; dir overridable with
`BROWSER_STATE_DIR`).

### Why Chromium, not Firefox

The original client was Firefox. Acting on a logged-in session needs a clean way to *capture* that
session once and *reuse* it. Research verdict (two independent passes, against Playwright's own
docs): **Chromium has a clean session-reuse story** (storage_state round-trips, shared profile, CDP
attach all work); **Firefox has no supported path** — storage_state can't be imported into a real
browser, `connectOverCDP` is Chromium-only, and Playwright ships a patched Firefox that can't share
the branded profile. So baski's `PlaywrightClient` now launches Chromium. It's nisse's only consumer
(clarity has its own copy), so the swap had no other blast radius.

### Capturing the session: `make startbrowser`

`make startbrowser U=<chat-id>` (→ `app/startbrowser.py`) opens a **headed** Chromium using the same
browser config the agent uses (so the saved fingerprint matches replay), you log into the services
you care about — and **save your payment card on those sites** — then press Enter and the session is
written to that chat's storage-state file. Re-run when a login expires (see "Honest limits").

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
accessibility tree so the agent always works from the current page:

- `web_open(url)` — navigate the logged-in session, return the a11y tree.
- `web_snapshot()` — re-read the current page (after an async change, or to find an element).
- `web_click(role, name)` — click by ARIA role + accessible name.
- `web_type(role, name, text, submit)` — type into a field; `submit` presses Enter.

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

Both env vars are **required** (fail-fast on missing, like every other env in the repo):

- `BROWSER_STATE_DIR` — directory holding the per-chat storage-state files.
- `BROWSER_PROXIES` — `host:port:user:pass` lines (the Webshare "download list" output). The provider
  API token stays out of the app — curl the list once and put the lines here.
- Browsers: `playwright install chromium` (Dockerfile installs it `--with-deps`).
