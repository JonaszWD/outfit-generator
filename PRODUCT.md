# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Personal use and a small group (you, friends, family), each with a private closet. Situations: deciding what to wear from clothes already owned, and deciding whether to buy a piece before purchase. Auth is currently a dev-user stand-in plus login/register/reset pages; no real weather yet.

## Product Purpose

Wardrobe lets a user upload photos of their clothes (background removed, tags suggested then confirmed), generates scored outfits with explanations, and, for a pasted product link or photo, reports how many new outfits that piece would unlock with the existing closet. Success: the user wears more of what they own and buys fewer pieces that add nothing.

## Positioning

The headline answer is "how many new outfits would this purchase unlock in YOUR closet", computed by a deterministic, explainable rules engine (hard rules, scoring, sanity table), not a black-box AI stylist. Every outfit and verdict can be traced to stated rules.

## Operating Context

- Closet grid with filters (category, color, season, search), HTMX "Load more".
- Batch upload, tag confirmation review queue, item detail/edit/delete.
- Outfit generation, planner, stats (most/least versatile pieces).
- Link analysis at `/analyze` and `/candidates`; candidates are items with `source=candidate`, never shown in closet, stats or generation until adopted.

## Capabilities and Constraints

- Stack: FastAPI, Jinja2 templates, HTMX, SQLModel/Postgres, Alembic; engine in `app/engine/` is pure Python (no DB/web imports).
- Scraper fetches only the pasted page, respects robots.txt, stores no page copy, no JS execution by default.
- Only confirmed-tag items feed the engine. Allowed tag values live in `app/engine/vocab.py`.
- Weather and real auth: not yet built.
- UI must follow DESIGN.md (project CLAUDE.md mandate).

## Brand Commitments

Product name: Wardrobe. Visual system is mandated by DESIGN.md in the repo (binding per CLAUDE.md); not expanded here.

## Evidence on Hand

Seed script with 25 placeholder items, demo scripts, docs in `docs/`. No real user testimonials, usage data or customer proof; do not fabricate.

## Product Principles

- Explainable over clever: show the rule or reason behind every outfit and verdict.
- The unlock count is the headline of any purchase analysis.
- Privacy by default: private closets, minimal retention of scraped data, EXIF stripped.
- Candidates never contaminate the owned closet.
- Tags are user-confirmed; AI only suggests.

## Accessibility & Inclusion

Responsive web, WCAG AA: contrast, keyboard operation, visible focus, labels on forms.
