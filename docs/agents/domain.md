# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root.
- **`docs/adr/`** — read ADRs that touch the area about to be changed.

If either location does not exist, proceed silently. The `/domain-modeling` skill creates the files lazily when terminology or architectural decisions are actually resolved.

## File structure

This repository uses a single-context layout:

/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-example-decision.md
│   └── 0002-example-decision.md
└── src/

## Use the glossary's vocabulary

When output names a domain concept—in an issue title, refactor proposal, hypothesis or test name—use the term defined in `CONTEXT.md`. Do not drift to synonyms the glossary explicitly avoids.

If the required concept is absent, reconsider whether new terminology is necessary or record the gap for `/domain-modeling`.

## Flag ADR conflicts

If proposed work contradicts an existing ADR, surface the conflict explicitly rather than silently overriding the decision.
