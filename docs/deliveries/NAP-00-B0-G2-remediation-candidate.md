# NAP-00 B0 Generation 2 Bounded Remediation Candidate

## Status

The one permitted G2 bounded remediation has been implemented and independently validated by the controller. It remains a review candidate only until the same Sol reviewer closes all frozen Findings.

## Exact identities

- Frozen review/Manifest commit: `8aa250f46fbf7f3064d8d60cc848bce3a7d8e8a2`
- Frozen Manifest SHA-256: `51ae0acacff613175c1c78afe33a198850e5a1e75217ad71c9e0cde73ffff139`
- Remediation source commit: `87f2ee7cdcf5181fe731f53d18b30c97db9f15ad`
- Remediation source tree: `12fe0ef120861eeca31527a5a52560c65e5435e7`
- Source parent: `8aa250f46fbf7f3064d8d60cc848bce3a7d8e8a2`
- Source commit paths: 31; out-of-scope paths: 0; evidence paths: 0

## Frozen Finding coverage

- `NAP-B0-G2-F-001`: TypeScript Result/Attempt identity fences and Plan context parity.
- `NAP-B0-G2-F-002`: unpaired-surrogate rejection and cross-language JCS parity.
- `NAP-B0-G2-F-003`: exact current+accepted Atom authority for Claim sealing.
- `NAP-B0-G2-F-004`: one canonical provider payload map for files.sha256 and package/release identity.
- `NAP-B0-G2-F-005`: contract-correct paged Asset reads with `next_offset=null` EOF.

No Finding was added, expanded or closed by an implementation worker.

## Controller validation

| Check | Result |
|---|---|
| SDK worker contracts | `33 passed` |
| Provider worker suite | `66 passed` |
| Integrated precommit | `124 passed, 2 commit-dependent deselected` |
| Locked TypeScript / compileall | PASS / PASS |
| Deterministic provider rebuild | `0` differences |
| Exact source postcommit suite | `126 passed in 28.52s` |
| Exact source gate | `status=ok`, exit 0 |
| Fresh `core.autocrlf=true` gate | `status=ok`, exit 0 |
| Fresh checkout complete suite | `126 passed in 29.84s` |
| Fresh checkout compileall/status | PASS / clean |

Machine-readable evidence is in [`coordination/NAP-00/b0-generation-2-remediation-evidence-v1.json`](../../coordination/NAP-00/b0-generation-2-remediation-evidence-v1.json).

## Targeted rereview handoff

Reuse reviewer `01a045f1-b0a7-7283-9dae-b548d1a1df58` for `NAP-B0-G2-F-001` through `NAP-B0-G2-F-005` only. New or expanded Findings are forbidden. Until 5/5 are `CLOSED`, `b0_acceptance_eligible=false` and `release_NAP_01_eligible=false`.
