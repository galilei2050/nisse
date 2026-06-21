# How the owner uses nisse — usage-scenario taxonomy

A catalog of how ONE owner uses a personal AI assistant, and — for each scenario — what it implies
for **keeping conversation history**. This is the usage model that drives the context-pruning
design (`docs/context-pruning.md`); it is deliberately a separate doc because the taxonomy is useful
on its own (memory design, prompt design, eval scenarios).

Derived from: two independent same-prompt agent passes building the catalog (consensus), plus two
Perplexity research passes for empirical grounding. nisse itself has too little usage to mine, so the
single-owner percentages are reasoned estimates; the published numbers (§4) are the external anchor.

---

## 1. The five retention classes (the load-bearing output)

Every scenario falls into one class by **what survives the turn and for how long**:

| Class | Definition | What to keep | What to drop |
|---|---|---|---|
| **DISPOSABLE** | Value consumed in the turn; goes stale immediately. | nothing | the whole exchange |
| **DURABLE-FACT** | Contains a lasting fact/preference. | the fact → long-term/core memory | the chatter, once the fact is saved |
| **ARTIFACT** | Produces a durable object that lives OUTSIDE the transcript (reminder, reservation, list, draft). | a pointer to the artifact (it's in its own store) | the dialogue, once the artifact exists |
| **KEEP-WHILE-ACTIVE** | Needs verbatim continuity DURING the session; stale after. | recent turns while the session is live | after the session closes |
| **REFERENCE-LATER** | Might be explicitly recalled later (research result, decision + rationale). | a compact summary/handle for recall | the raw transcript |

The split that matters for pruning is **when the value is consumed**: DISPOSABLE/ARTIFACT/saved-FACT
are spent almost immediately; KEEP-WHILE-ACTIVE is consumed *during* the session; only
REFERENCE-LATER needs cross-session *retrieval*.

### Estimated single-owner mix (consensus of both catalog passes)
| Class | ~% of turns | Pruning implication |
|---|---|---|
| DISPOSABLE | ~40% | evict right after the turn; never embed/persist |
| ARTIFACT | ~20% | artifact persists elsewhere; evict dialogue after confirmation |
| DURABLE-FACT | ~15% | extract fact at write time, then drop the chatter (highest leverage: tiny payload, permanent value) |
| KEEP-WHILE-ACTIVE | ~15% | keep verbatim only within the live session; expire on close |
| REFERENCE-LATER | ~10% | summarize to a recall handle; evict raw transcript |

**Net:** ~75% of turns (DISPOSABLE + ARTIFACT + the spent half of DURABLE-FACT) can leave working
context almost immediately once their durable residue, if any, is extracted. Only ~25%
(KEEP-WHILE-ACTIVE + REFERENCE-LATER) justifies holding real conversational text — and
KEEP-WHILE-ACTIVE is short-lived. "Venting" is **one** member of DISPOSABLE, not the whole story.

---

## 2. Scenario catalog by life domain

One example utterance each; tag in **bold**. (Merged & deduped from both catalog passes.)

**Food & cooking** — "Собери список продуктов на неделю" **ARTIFACT** · "Что приготовить из курицы и риса?" **DISPOSABLE** · "Я вегетарианец, без грибов" **DURABLE-FACT** · "Проведи по рецепту шаг за шагом" **KEEP-WHILE-ACTIVE** · "Напомни разморозить курицу в 18:00" **ARTIFACT** · "Терпеть не могу кинзу" **DURABLE-FACT**

**Shopping / purchases** — "Найди тихую механическую клавиатуру до $120" **REFERENCE-LATER** · "Добавь батарейки и зубную пасту в список" **ARTIFACT** · "Мой размер L, обувь 43" **DURABLE-FACT** · "Следи за ценой на монитор, пингани если <$300" **ARTIFACT** · "Я уже купил подарок папе?" **REFERENCE-LATER**

**Travel & bookings** — "Забронируй столик в итальянском на пятницу, на двоих" **ARTIFACT** · "Спланируй 4 дня в Лиссабоне" **REFERENCE-LATER** (KEEP-WHILE-ACTIVE во время) · "Найди рейсы в Берлин до €200" **KEEP-WHILE-ACTIVE→ARTIFACT** · "Предпочитаю место у прохода" **DURABLE-FACT** · "Какой у меня номер брони?" **REFERENCE-LATER**

**Scheduling / reminders** — "Напомни позвонить дантисту завтра в 10" **ARTIFACT** · "Что у меня в календаре сегодня?" **DISPOSABLE** · "Перенеси 15:00 на 16:00" **ARTIFACT** · "Каждое воскресенье — вынести мусор" **ARTIFACT** · "День рождения мамы 3 мая" **DURABLE-FACT**

**Work / productivity** — "Набросай вежливый отказ от встречи" **ARTIFACT** · "Сделай саммари этого документа" **REFERENCE-LATER** · "Помоги продумать архитектуру X" **KEEP-WHILE-ACTIVE** · "Я бэкенд-разработчик в финтехе, менеджер — Sara" **DURABLE-FACT** · "Какие были мои цели на Q3?" **REFERENCE-LATER**

**Learning / research** — "Объясни CRISPR как 12-летнему" **DISPOSABLE** · "Исследуй лучшие эргономичные кресла, с источниками" **REFERENCE-LATER** · "Погоняй меня по испанским глаголам" **KEEP-WHILE-ACTIVE** · "Я учу гитару, средний уровень" **DURABLE-FACT**

**Health & fitness** — "Составь силовую программу 3×/нед" **REFERENCE-LATER** · "У меня аллергия на пенициллин, астма" **DURABLE-FACT** (safety-critical) · "Вес 78, цель 72" **DURABLE-FACT** · "Напомни принять лекарство в 21:00" **ARTIFACT** · "Плохо спал, чувствую себя разбито" **DISPOSABLE** (состояние, не черта)

**Finance** — "Сколько могу тратить, если откладывать 20%?" **DISPOSABLE** · "Отслеживай подписки, флагай неиспользуемые" **ARTIFACT/REFERENCE-LATER** · "Мой бюджет €2500/мес" **DURABLE-FACT** · "Объясни индексные фонды" **DISPOSABLE** · "Напомни про аренду 1-го числа" **ARTIFACT**

**Home / errands** — "Как отмыть красное вино с ковра?" **DISPOSABLE** · "Вызови сантехника в субботу" **ARTIFACT** · "Мой роутер Netgear R7000" **DURABLE-FACT** · "Добавь молоко, скотч в список" **ARTIFACT**

**Relationships & social** — "Помоги выбрать подарок девушке" **KEEP-WHILE-ACTIVE→ARTIFACT** · "Партнёра зовут Anna, годовщина 12 июня" **DURABLE-FACT** · "Сформулируй извинение другу" **ARTIFACT** · "Стоит ли поговорить с коллегой про X?" **KEEP-WHILE-ACTIVE** · "Запомни: друг Дима разводится" **DURABLE-FACT**

**Emotional support / venting** — "Пожаловаться на бывшую" **DISPOSABLE** (катарсис — ценность, не транскрипт; вспомнить это через неделю — жутко) · "Тревожно перед собеседованием завтра" **DISPOSABLE** (+ «собеседование завтра» кратко DURABLE-FACT) · "Помоги прожить горе" **KEEP-WHILE-ACTIVE** · "У меня в целом тревожность" **DURABLE-FACT** (формирует тон)

**Entertainment / recommendations** — "Посоветуй триллер как Se7en" **DISPOSABLE** · "Обожаю Dune, ненавижу ромкомы" **DURABLE-FACT** · "Что ты советовал в прошлый раз?" **REFERENCE-LATER** · "Добавь фильм в вотчлист" **ARTIFACT**

**Communication drafting** — "Перепиши, чтобы звучало мягче" **DISPOSABLE** (вывод вставляют наружу) · "Пост в LinkedIn про повышение" **ARTIFACT** · "Мой стиль — кратко, без эмодзи" **DURABLE-FACT**

**Quick facts / trivia** — "Сколько времени в Токио?" **DISPOSABLE** · "Сколько унций в чашке?" **DISPOSABLE** · "Столица Монголии?" **DISPOSABLE**

**Smalltalk / companionship** — "Доброе утро! Как ты?" **DISPOSABLE** · "Расскажи анекдот" **DISPOSABLE** · "Зови меня Sam / я зову тебя Nisse" **DURABLE-FACT**

**Decisions / advice** — "Брать ли оффер? Плюсы/минусы" **KEEP-WHILE-ACTIVE→REFERENCE-LATER** · "Выбрать между двумя квартирами" **KEEP-WHILE-ACTIVE→REFERENCE-LATER**

**Creative tasks** — "Напиши поздравительный стих сестре" **ARTIFACT** · "Накидай названия для подкаста" **KEEP-WHILE-ACTIVE→ARTIFACT** · "Помоги с планом рассказа" **KEEP-WHILE-ACTIVE**

A recurring shape: many scenarios **start** KEEP-WHILE-ACTIVE (deliberation) and **end** as an
ARTIFACT or REFERENCE-LATER (the decision) — the live text matters during, a compact residue after.

---

## 3. The hard cases (what a pruner must NOT throw away)

- **KEEP-WHILE-ACTIVE mid-session** — forgetting here is catastrophic (the assistant looks like it
  has amnesia *during* the conversation). This is what a recency window protects.
- **DURABLE-FACT not yet saved** — a constraint ("вегетарианец", "аллергия на пенициллин") stated
  once; if it scrolls out before being written to memory, future answers are wrong/unsafe. Memory's
  job, not the transcript's — but the window must be generous enough not to outrun the save.
- **REFERENCE-LATER** — the only class needing cross-session *retrieval* (research results,
  decision + rationale). Compact handle, not raw transcript.

Everything else (DISPOSABLE, spent ARTIFACT, saved DURABLE-FACT) is safe to drop quickly.

---

## 4. Empirical grounding (published data — anchors, with confidence flags)

No nisse usage data exists; these external numbers set priors. **Each rests on a single
dataset/survey unless noted** — treat as directional, not precise.

**ChatGPT message logs — OpenAI/NBER "How People Use ChatGPT" (2025), very large sample, internally
consistent:**
- **Asking 49% · Doing 40% · Expressing 11%** of messages. Asking+Doing = **89% instrumental**.
- Topic level: Practical guidance, Information seeking, Writing ≈ ¾ of all messages. **Self-expression
  topic 4.3%; relationships/personal-reflection sub-topic only 1.9%.**
- **~70% of messages are non-work**; within work, writing ≈40% (≈⅔ editing existing text).

**Claude logs — Anthropic Economic Index:** concentrated in software-dev + technical writing; 57%
augmentation vs 43% automation. (Skews technical — a different population than a personal life bot.)

**Pew "Americans and AI 2026" (n=5,119 US adults; single survey):** ~½ use chatbots, ~25% daily.
Self-reported reasons: **info search 42% · work 38% · fun 25% · image/video 24% · medical 20% ·
diet/fitness 20% · news 13% · emotional support 10% · companionship 4%.**

**Voice assistants (YouGov / Pew 2017; qualitative + industry):** dominated by short single-turn
utility — weather 59%, music 51%, quick answers 47%, timers 40%; sharp drop to smart-home 19%,
shopping 14%. Essentially no companionship use.

**Companion bots (Replika, Pi; small, self-selected samples):** interactions are predominantly
multi-turn, memory-dependent, relationship-framed — but a small, non-representative subset of users.

### Ephemeral one-off vs. memory-dependent — the key question
**No provider publishes a direct "requires cross-session memory" split — it is inferred, not
measured (a genuine gap).** What the evidence supports:
- General chatbots: the dominant **~80–90%** (Asking+Doing) is episodic, answerable in one/few turns
  without prior-session memory. The memory-leaning slice is **~10% (±)**, bounded by Expressing (11%)
  and corroborated by ~10% of adults reporting emotional-support use — concentrated in
  emotional-support/companionship.
- The user-vs-message gap reconciles cleanly: many people *try* emotional support, but it's a tiny
  share of total *messages* (1.9%).
- Voice assistants: overwhelmingly ephemeral, single-turn.
- Companion bots: inverted (≈100% relational within those samples).
- Multi-turn ≠ memory-dependent: "LLMs Get Lost in Multi-Turn Conversation" (arXiv 2505.06120) finds
  ~**39% performance drop** on multi-turn underspecified tasks — a reason users keep complex tasks in
  one well-specified prompt rather than a long thread.

**Takeaway:** the empirically dominant demand is instrumental, short-session, low-memory — matching
the ~75%-prunable estimate in §1. Cross-session memory matters for a real but minority slice (~10%),
exactly where a *personal* assistant differentiates, so the design must serve it without holding the
whole transcript.

---

## 5. Implication for context pruning

The taxonomy says the bulk of turns carry no lasting value or collapse to a tiny durable record kept
elsewhere (memory / reminders / lists). So the pruning design does NOT need topic-modelling or
summarization to be correct — it needs to **keep the live session window, let durable residue go to
memory at write time, and drop the rest**. See `docs/context-pruning.md` for the chosen
deterministic-window approach, the red-team guardrails, and code touch-points.

---

## 6. Sources

**Strongest (large samples, but each a single dataset — not independently re-estimated):**
OpenAI/NBER "How People Use ChatGPT" (2025); Anthropic Economic Index; Pew "Americans and AI 2026"
(n=5,119).

**Supporting (single survey / smaller / industry):** Pew teens AI survey; Pew 2017 voice assistants;
YouGov digital-assistant tasks; WildChat (arXiv 2405.01470); Replika user survey (small, self-selected);
Character.AI analysis (arXiv 2505.13354).

**Mechanism (not usage shares):** "LLMs Get Lost in Multi-Turn Conversation" (arXiv 2505.06120).

> arXiv IDs are as surfaced by the research passes; WildChat 2405.01470 is verified, the others are
> plausible but not independently confirmed here. The **ephemeral-vs-memory split is inferred, not
> directly measured in any public dataset** — the single largest evidence gap. Investigation scratch:
> `scratch/context-pruning-brainstorm.md` (git-ignored).
