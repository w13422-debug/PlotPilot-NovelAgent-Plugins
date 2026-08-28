# NAP-00 B0 Generation 2 Candidate

## Status

G2 source assembly and controller validation are complete. This is a review candidate only: fresh Sol/max full A–J review is still pending, so `b0_acceptance_eligible=false` and `release_NAP_01_eligible=false`.

## Exact source identity

- Branch: `codex/nap-00-b0-g2`
- Baseline: `c1b9519c7d25ce1fbef07984cdb548c89e7e1152`
- Generation parent: `3cfd6619959c16d394655bda170557b96052e6c9`
- Source commit: `eae0b7a4b58a94ae9a5a78ca9b20349fee65217c`
- Source tree: `ac421d0e680e5387ba6cd1059c6f3b029b80808d`
- Source-commit changed paths: 62; evidence paths in source commit: 0
- Baseline-to-source changed paths: 239
- Frozen Finding Manifest blob: `75be4d5a52f4ed60b2901df329e7b72236887ff6`

The rejected G1 source `db434847...` and evidence `480d0f46...` are both confirmed non-ancestors of this source candidate.

## Delivered behavior

- Candidate staging is private until atomic publication; failed/skipped/invalid/conflicting attempts leave the visible set unchanged.
- Python and TypeScript enforce the same Candidate workspace, parent, cycle and partial-result semantics.
- Anthropic and Gemini providers bind descriptor references to an exact closed schema index and raw schema SHA-256 values.
- Provider package, wheel, receipt and release identities rebuild deterministically.
- Demo package line endings are pinned for Windows fresh checkouts.
- The exact-source gate enforces G2 parentage, rejected-generation exclusion, frozen manifest identity, write-set bounds, package/schema identities and locked TypeScript compilation.

## Controller validation

| Context | Result |
|---|---|
| Precommit integrated suite | `105 passed, 2 deselected` |
| Deterministic provider rebuild | `0` byte/hash differences |
| Exact source postcommit suite | `107 passed in 29.87s` |
| Exact source integration gate | `status=ok`, exit 0 |
| Fresh clone | exact source/tree; `core.autocrlf=true`; initially clean |
| Fresh clone official gate-first run | locked `npm ci` + TypeScript check PASS; gate exit 0 |
| Fresh clone complete suite | `107 passed in 28.46s` |
| Fresh clone compileall | exit 0 |
| Fresh clone final Git status | clean |

The fresh clone was also tried once in the wrong order (pytest before the locked `npm ci`): 106 tests passed and the sole failure reported missing local `tsc`. The official gate-first sequence installed the lockfile-pinned compiler and then all 107 tests passed. This setup observation did not change tracked source.

Machine-readable commands, identities and raw result summaries are in [`coordination/NAP-00/b0-generation-2-evidence-v1.json`](../../coordination/NAP-00/b0-generation-2-evidence-v1.json).

## Review handoff

Candidate is ready for a **fresh, read-only `gpt-5.6-sol / max` full A–J review**. Controller and implementation self-tests do not constitute Sol PASS, do not close Findings, and do not release NAP-01.
