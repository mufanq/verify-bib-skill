---
name: verify-bib
description: "Verify a BibTeX file against Semantic Scholar to catch AI-hallucinated citations before submission. Use when the user has a .bib file they want to check, is writing LaTeX and wants citations verified, or suspects AI-generated references. Flags fake papers, wrong authors, mismatched venues."
metadata:
  version: 0.1.0
  openclaw:
    category: "research"
    requires:
      bins:
        - python3
---

# verify-bib

Verify every entry in a `.bib` file against Semantic Scholar (214M+ papers). For each entry, computes title / author / venue similarity scores so you can spot hallucinated or corrupted citations.

## When to trigger this skill

- The user asks to "verify", "check", or "validate" a `.bib` file.
- The user is preparing a LaTeX manuscript for submission and wants to confirm references are real.
- The user mentions AI-generated citations and wants them vetted.
- The user pastes a BibTeX block and asks whether the papers exist.

## Usage

```bash
python3 ~/agent/scripts/verify-bib-skill/verify_bib.py path/to/references.bib
python3 ~/agent/scripts/verify-bib-skill/verify_bib.py refs.bib --json     # machine-readable
```

## Setup (once)

```bash
pip install -r ~/agent/scripts/verify-bib-skill/requirements.txt
```

### API key is optional — two modes

| Mode | Config | Throughput |
|------|--------|-----------|
| **No key (default)** | Nothing to do | Shared 5000 req / 5 min pool. Script paces itself at 5 req/s. |
| **With key** | `export SEMANTIC_SCHOLAR_API_KEY=...` in `~/.zshrc` | Dedicated 1 req/s. More reliable during peak hours. |

The script **never prompts** for a key. It reads `SEMANTIC_SCHOLAR_API_KEY` from the environment; if absent, it falls back to unauthenticated requests. To override at the command line: `--api-key sk_...`.

Request a free key at https://www.semanticscholar.org/product/api.

## How to interpret output

Each entry returns three scores in `[0, 1]`:

| Score | Meaning |
|-------|---------|
| `title_score` | Fuzzy-match of the entry title vs. the closest Semantic Scholar paper. `≥ 0.85` ⇒ `title_match = true` ⇒ the entry is `verified`. |
| `author_score` | Overlap of last-name sets. `≥ 0.5` ⇒ `author_match`. |
| `venue_score` | Fuzzy-match of journal / conference name. `≥ 0.6` ⇒ `venue_match`. |

Verdict priority:
1. **`verified = false, error = "not found"`** — very likely hallucinated. Flag hard.
2. **`verified = true` but `author_match = false`** — real paper exists but the BibTeX authors are wrong (common when BibTeX was auto-generated from a bad source).
3. **`verified = true, author_match = true, venue_match = false`** — real paper but venue field is sloppy (e.g. `NIPS` vs `NeurIPS`). Usually safe.
4. **All three true** — entry is trustworthy.

## Caching

Results are cached for 30 days in `~/.cache/verify-bib/s2_cache.sqlite` keyed by normalized title, so re-runs on the same `.bib` are instant.

## Exit codes

- `0` — every entry verified cleanly.
- `1` — at least one entry failed verification.
- `2` — input file not found.

Useful as a pre-submission CI check: `verify_bib.py refs.bib && latexmk paper.tex`.

## Output example

```
📚 Verified 12 entries

  ✅ Verified:     9
  ⚠️  Mismatched:   2
  ❌ Not found:    1

❌ smith2024fake
   Title : Quantum Neural Bifurcation of Hyperbolic Attention Models
   Error : not found in Semantic Scholar

⚠️ vaswani2017
   Title : Attention Is All You Need
   → S2  : Attention Is All You Need  [t=1.00 a=0.38 v=0.57]
   Authors mismatch: input='Vaswani and Shazeer and Parmar'
                     S2   =['Ashish Vaswani', 'Noam Shazeer', ...]
```

## Notes

- BibTeX entry types `@comment`, `@preamble`, `@string` are skipped.
- `@misc` entries are included (unlike TrueCite, which skips them) since many arXiv-only references use this type.
