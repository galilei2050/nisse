# docs/ — design notes & research

Deeper design notes, research, and test-case docs. Filenames describe the topic; check here before
implementing.

## What belongs in a doc

**Docs hold DURABLE facts, not fluid ones.** Write down the durable reasoning — research findings, the
*why* behind a decision, structural models, trade-offs, the usage model. Do NOT store fluid facts that
rot:

- current config values (constants, budgets, thresholds, model ids),
- "shipped / now does X" current-state claims,
- deploy mechanics and other operational specifics,
- exact point-in-time measurements presented as standing truth.

**Never duplicate discoverable code** — the code is the source of truth for current values. Instead
**link/reference** it (file, symbol, "see `_MAX_TOKENS` in the code") so the doc stays correct as the
code changes. A measured number is fine as *evidence of research* — label it a dated snapshot when its
absolute value will drift; the durable takeaway is usually the ratio/direction, not the absolute.
