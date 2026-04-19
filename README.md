# verify-bib

A Claude Code skill (also usable as a standalone CLI) that catches AI-hallucinated citations in BibTeX files. Inspired by [TrueCite](https://wispaper.ai/agents/true-cite), built on [Semantic Scholar](https://www.semanticscholar.org/product/api).

## Why

LLMs confidently fabricate plausible-looking citations. Running a paper draft through this tool surfaces:

- **Pure hallucinations** — papers that don't exist anywhere.
- **Author corruption** — real paper, wrong author list.
- **Venue sloppiness** — real paper, venue field garbled (e.g. arXiv preprint cited as a journal article).

## Install

```bash
git clone https://github.com/WhenMelancholy/verify-bib-skill.git
cd verify-bib-skill
pip install -r requirements.txt
```

To use as a Claude Code skill, symlink into your skills directory:

```bash
ln -s "$(pwd)" ~/.claude/skills/verify-bib
```

## API key is optional

Runs out of the box without a key (shared 5000 req / 5 min pool). For higher throughput request a free key at https://www.semanticscholar.org/product/api and:

```bash
export SEMANTIC_SCHOLAR_API_KEY=your_key_here   # ~/.zshrc or ~/.bashrc
```

The script **auto-detects** whether a key is set:

- **No key** → unauthenticated requests, 0.2 s sleep between entries.
- **Key present** → `x-api-key` header, 1.05 s sleep between entries.

You can also pass `--api-key sk_...` on the command line to override. The key is never logged or committed — `.env` and common secret files are in `.gitignore`.

## Use

```bash
# Human-readable report
python3 verify_bib.py references.bib

# Machine-readable (pipe into jq, CI, etc.)
python3 verify_bib.py references.bib --json
```

Exit codes: `0` all clean, `1` issues found, `2` file not found. Suitable as a pre-submission gate.

## How it works

1. Parse `.bib` with pybtex.
2. For each entry, query Semantic Scholar's `/paper/search/match` with the title (fall back to `/paper/search` if the match endpoint returns nothing).
3. Compute three fuzzy scores (rapidfuzz token-set ratio on title & venue, last-name set overlap on authors).
4. `verified = title_score ≥ 0.85`. Author / venue scores are surfaced as additional flags.
5. Cache successful lookups in `~/.cache/verify-bib/s2_cache.sqlite` for 30 days.

## Design mirrors TrueCite

The scoring + judgment model follows the reverse-engineered behavior of
[wispaper.ai/agents/true-cite](https://wispaper.ai/agents/true-cite) — title match is the primary verdict, author / venue mismatches are surfaced as secondary flags rather than hard failures. This matches how real BibTeX files drift: the paper is usually real, but author lists and venue strings are often truncated or auto-generated from lossy sources.

## License

MIT
