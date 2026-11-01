# Development and offline checks

A Python 3.12+ MCP server for Proton Mail Bridge, built with the official Python
[`mcp` SDK / FastMCP v1](https://py.sdk.modelcontextprotocol.io/v1/).
This repository supplies code and an example LaunchAgent. Building or testing
the repository does not install anything on a Mac or connect to real mail. The optional
[installer](install.md) sets up Bridge, credentials and services only when you run it, and
`--dry-run` prints its plan first. Installer-specific tests are described under
[Tests and CI](install.md#tests-and-ci).

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
make check
# Or select an installed interpreter and formatter explicitly:
make check PYTHON=/path/to/python3.14 RUFF=/path/to/ruff
```

`make check` runs lint and the full suite before merging, on up to twelve
[pytest-xdist](https://pytest-xdist.readthedocs.io/) workers. Set `JOBS=0` for serial debugging
or `JOBS=4` to limit concurrency, and `PYTEST_ARGS` to pass pytest options;
`PYTHON` and `RUFF` select existing tools.
Temporary test state uses the system temporary directory; `TMPDIR` can select
another directory outside Git, as the search index refuses cache paths in a checkout.
[ci.md](ci.md) says what GitHub Actions runs and when.

`make docs` regenerates [`docs/tools.md`](tools.md) from the registered tools; run it after
changing a tool's docstring or arguments, or the server instructions. `make check` includes
[`tests/test_docs.py`](../tests/test_docs.py), which fails when that page is stale, when a link,
anchor or `llms.txt` entry is broken, when a setting or tool is missing from its page, or when text
a model reads outgrows what clients keep. A docs-only pull request runs no CI, so run
`.venv/bin/python -m pytest tests/test_docs.py` first. [AGENTS.md](../AGENTS.md) says where each
kind of text belongs.

Tests use fake IMAP connections
and real local Unix socket pairs speaking IMAP through the standard library parser.
Tests do not access real mail, Keychain credentials, or external network endpoints.
Coverage includes bearer authentication, TLS restrictions, safe search
criteria, thread/body extraction, read-only behavior, draft append, secret
redaction, single-use preview tokens, Sieve script escaping and parse-back
verification, cap/protection checks, append-only logging,
undo, deadline partial outcomes, command allowlists, and a package scan confining
SMTP to the guarded send module. Fake SMTP and elicitation tests cover send guards;
socket tests exercise
preview → confirmation → undo for every cleanup action. These tests do not validate
behavior against a live Bridge.

Preview speed regressions use 45 and 200 matching messages amid thousands of
unrelated messages, with deterministic per-command latency. They check bounded
round trips, batched header-only PEEK, unchanged client matching, identity
ambiguity, and refusal when preview or execute headroom is genuinely missing.
The larger mailbox fixtures also exercise confirmation, undo and deadline drift.

The fallback path used by Bridge replaces candidate-only
cost limits with bounded full-mailbox discovery. Complete envelope scans preserve
old/protected-copy and ambiguity checks; rules also need complete raw identity
audits to detect selected identities hidden in second fields. The All Mail
rules-preview raw fallback uses UIDNEXT-derived FETCH ranges of at most 200 UIDs,
without SEARCH ALL or HEADER searches, and saves progress after each range for continuation. Both
scans use bounded batches and caches, so fetched rows and commands
grow at most linearly with the mailbox views inspected. The speed tests check
two mailbox sizes and retain the previous limits wherever they still pass.

The tests below state their bounds as assertions. They run on synthetic mailboxes, and the
certified results require synthetic CONDSTORE support, so none of this describes Bridge
performance.

For the fixed overlapping-folder fixtures, let `M` be the physical mailbox size, `C` the
candidate count (at most 200) and `S` the number of source rows.
[`tests/test_preview_speed.py`](../tests/test_preview_speed.py) asserts these bounds for an
ordinary preview, with `M` of 240 and 6,200:

| Measure | Bound |
| --- | --- |
| Fetched DB rows | `ceil(3.5*M) + 2*C` |
| `UID FETCH` commands | `max(8, ceil(3.45*M/1000 + 4)) + max(0, ceil(S/200) - ceil(C/200))` |
| All commands | The `UID FETCH` bound plus 50 |
| Modeled time | Under 80% of the call budget, asserted at `M` of 6,045 and 6,200 |

The 45- and 200-match fixtures also stay within 90 commands and `S + 4*C` raw header rows. At
`M` of 6,045, with 100 ms per command and 1.7 ms per fetched DB row, a 45-message move preview
stays under 50 modeled seconds. A rules preview uses a separate bound linear in the mailbox: each
call stays under 20 modeled seconds, follows `next_cursor` to the end within ten calls, and
fetches at most 200 UIDs per command. These formulas describe the fixture's fixed folder
layout; production scans each relevant mailbox view within the call budget.

[`tests/test_identity_index_integration.py`](../tests/test_identity_index_integration.py) covers
the certified path:

| Case | Bound |
| --- | --- |
| Warm ordinary preview | Beyond the source scan, exactly `3*C` fetched DB and raw rows and at most 3 `UID FETCH` commands; at most 85 commands; net of that scan, under 15 modeled seconds for 45 candidates and under 25 for 200 |
| Warm subject rules | Per call, at most 7 `UID FETCH` commands, at most 90 commands and under 20 modeled seconds |
| Cold build, ordinary preview | The ordinary row and `UID FETCH` bounds above, plus at most six more commands per folder; under 80% of the call budget |
| Cold build, rules preview | Each call under 20 modeled seconds, resuming with `next_cursor` |

Warm reuse is available only after a complete certified build succeeds. UID-set enumeration and
local index processing remain mailbox-sized; these bounds concern expensive fetched metadata and
total modeled protocol cost. These results do not claim warm speed on servers lacking certifiable
CONDSTORE evidence.

[`tests/test_preview_identity_cost.py`](../tests/test_preview_identity_cost.py) uses a
50,000-message model: a `HEADER` identity search costs 6 ms per row, an ENVELOPE row 20
microseconds and a raw header row 3 ms. Previews for 3, 45 and 200 matches return a token in under
22 modeled seconds, reading at least 50,000 ENVELOPE rows and at most five raw rows per match,
with no `HEADER Message-ID` search. Larger audits still refuse if evidence cannot finish in time.
Every command remains bounded by the remaining I/O budget, with the response
reserve and close-on-timeout behavior described in [Recovery](RECOVERY.md).

Raw Message-ID checks now require exact, unique canonical agreement on every
phase. Both formerly accepted duplicate-field audit cases refuse, alongside
missing, unrelated, unparseable and invalid UTF-8 fields.

These are synthetic measurements of client commands and fetched candidates.
They include a 50,000-message fake that charges mailbox-wide decryption cost for
`HEADER` searches and cheaper database metadata reads for envelope discovery.
They do not establish a live server's cost: complete metadata and raw-header
audits can still exceed a budget and must refuse safely.
Real mailbox latency still needs an operator test. Broad
`search_mail` header scans stop at the uncached candidate cap and report partial
results; explicit `since`/`before` bounds can reduce that work in large folders.

`PMM_REAL_BATCH` can optionally point to a
local JSON list of 27 Message-IDs; only the socket simulation uses those IDs, and
the test reports counts and timing without printing them. Optional comparison
and real-file cases skip when their input paths are not supplied.
