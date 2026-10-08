# Style references

An optional, AI-assisted layer on top of the rules engine. The engine still decides what is a
valid outfit and what it scores; reference photos only change the **order** of suggestions.

## Flow

1. `/settings/style`: upload up to 4 photos at a time, 12 in total. Each is validated, EXIF-stripped
   and re-encoded like closet photos, then read by the configured backend.
2. The model returns a `StyleResult`: 1-5 palette `colors`, `patterns`, `subtypes`
   (garments), and a one-line `summary`. Anything outside `app/engine/vocab.py` is rejected; one
   retry with the validation error, then the photo is refused with a readable message.
3. `build_profile` merges the results (share of photos showing each color, pattern and garment).
4. On `/outfits`, ticking **Match my reference photos (AI)** passes the profile to
   `generate_outfits(style=...)`. Candidates are still the valid outfits above `min_score`;
   each is re-ranked by `base + shift`, where the shift is in `[-0.2, +0.2]`.

## The match

| Part | Weight | Meaning |
|---|---|---|
| color | 0.40 | Mean over pieces of the best palette match (CIE76 distance within 25, scaled by how often the color appears) |
| pattern | 0.15 | Share of pieces whose pattern appears in the references |
| subtype | 0.15 | Share of pieces whose garment type appears in the references |

A match of 0.5 shifts nothing; 1.0 gives +0.2; 0.0 gives -0.2. Cards show "Adjusted by your style
references" plus reasons ("Colors echo your reference photos (navy, white)"). Tune
`StyleConfig` in `app/engine/style.py`.

## Comparing on and off

The checkbox state is remembered (`user_preferences.use_style_references`) but every request sends
it explicitly, and the results panel says "Ranking: with your reference photos" or "standard rules
only". Same closet, same context, tick and untick, press Regenerate. Counts and `/closet/stats`
are unaffected.

## Backends (`STYLE_BACKEND`)

| Value | Where the photo goes | Notes |
|---|---|---|
| `fake` (default) | nowhere | Placeholder style from the pixels and a hash; for tests |
| `claude` | Anthropic API (EXIF-stripped) | `STYLE_MODEL`, needs `ANTHROPIC_API_KEY` |
| `ollama` | nowhere (local) | `OLLAMA_URL`, `OLLAMA_MODEL` (default `qwen2.5vl:7b`) |
| `gemini` | Google Gemini API (EXIF-stripped) | `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-3.5-flash-lite`; copy the exact ID from AI Studio) |
| `openai` | Any OpenAI-compatible host | `STYLE_BASE_URL`, `STYLE_API_KEY`, `STYLE_MODEL` |

On Google's free tier, check whether your inputs may be used to improve their products; reference photos show other people.

### Local model

```bash
brew install ollama        # already installed on this machine
ollama serve               # or launch the Ollama app
ollama pull qwen2.5vl:7b   # alternatives: gemma3:12b, llama3.2-vision:11b
# .env: STYLE_BACKEND=ollama
```

Structured output is enforced by Ollama's `format` JSON Schema, then validated by the same
vocabulary check as the Claude backend. Extraction runs once per reference photo, not per
suggestion, so a slow local model only costs time at upload (not measured yet: try one photo and time it). Small models are weaker at naming garments and colors exactly; check
the summary shown under each reference and remove photos that were misread.

## Limits

Prototype. Styles are read from the photo only (no brand, fit or fabric). The profile is a blend:
two very different references produce a muddled profile rather than two looks. References are
per user and deleted with the account.
