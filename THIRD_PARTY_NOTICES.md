# Third-party notices

P0 does not vendor or copy third-party source code.  The PlotPilot baseline's
existing license text is retained in [`LICENSE`](LICENSE).  M0 tooling uses
the following external runtimes/libraries:

- Python: `jsonschema`, `rfc8785`, `pytest`, `fastapi`, `uvicorn`, `pydantic`,
  `httpx`, and their runtime dependencies.
- Frontend: the packages declared by `frontend/package.json` and resolved by
  `frontend/package-lock.json`/`pnpm-lock.yaml`.
- Browser evidence: Playwright and its bundled Chromium runtime.

## Unicode data

`contracts/unicode-casefold-v1.json` contains generated Unicode case-folding and
canonical-composition data derived from CPython `unicodedata` backed by the
Unicode Character Database (UCD) **15.0.0**.  The generation source is
`tools/integration/generate_unicode_casefold.py`; the checked-in generated
artifact is validated with that generator's `--check` mode.  This data is
covered by the **Unicode Data Files and Software License**, available at
<https://www.unicode.org/license.txt>.  The corresponding machine-readable
source/version/artifact/license record is in
`docs/deliveries/PPA-00/license-ledger.json`.

The exact versions, license metadata, upstream URLs, package integrity values,
and the complete lock-resolved npm package inventory are machine-recorded in
`docs/deliveries/PPA-00/license-ledger.json`.  A missing or ambiguous license
entry is a merge blocker; it must be resolved with a Dependency Delta and an
updated ledger before integration.
