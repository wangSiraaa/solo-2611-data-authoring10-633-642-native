# Purpose-authorised data service + anonymous microdata publisher

Python 3.11, standard library only (SQLite via `sqlite3`). The material in
`../case.json` is the sole source of finite domains and generalisation
hierarchies. Nothing here claims real-world anonymity or legal compliance:
K/D + generalisation is a fixed synthetic contract.

## Layout

```
semantic_microdata/
  semantic_microdata/
    domains.py        finite domains, ancestor chains, tree validation
    data_service.py   original records, revisions, retractions, grants,
                      prefix views, single global commit order
    optimizer.py      pure exhaustive level-vector enumeration, K/D groups
    publisher.py      draft / human review / atomic release, files + ledger
    plan.py           declarative JSON plan executor
    cli.py            command-line entry points
    schema.py         SQLite schema (one DB, one WAL)
    material.py       validated case.json loader
    errors.py         stable error codes + reasons
  examples/
    scenarios.py      ten worked scenarios (all contract corner cases)
    plans/*.json      declarative plans (main, ordering, review, ...)
  verify/
    reference_check.py  independent verifier (does not import optimizer)
    run_all.py          runs every plan, verifies every release
  tests/test_contract.py
```

## Quick start

```bash
cd semantic_microdata
PYTHONPATH=. python3 -m unittest discover -s tests -v   # 31 tests
python3 examples/scenarios.py                            # ten scenarios
PYTHONPATH=. python3 verify/run_all.py                   # plans + verifier
```

CLI:

```bash
python3 -m semantic_microdata.cli bootstrap --material ../case.json --db state.db
python3 -m semantic_microdata.cli view --material ../case.json --db state.db \
    --k 9 --purpose research
python3 -m semantic_microdata.cli draft --material ../case.json --db state.db \
    --releases rel --request-id r1 --k 9 --purpose research --K 2 --D 2 --weights 1,0,0
python3 -m semantic_microdata.cli review --material ../case.json --db state.db \
    --releases rel --draft-id d-... --decision approve --reason ok
python3 -m semantic_microdata.cli release --material ../case.json --db state.db \
    --releases rel --draft-id d-... --release-id rel-1
python3 -m semantic_microdata.cli plan --material ../case.json --db state.db \
    --releases rel --plan examples/plans/main.json
python3 verify/reference_check.py --material ../case.json --db state.db \
    --release-id rel-1
```

## Service semantics

* `put(record_id, subject_id, revision, row, retracted=False)` — highest
  revision wins; a later **lower** revision is refused (`STALE_REVISION`) and
  never resurrects the old row. A retraction (`row=None`) records an operation
  but leaves every prior original readable in history.
* Same record + same revision + same content = idempotent replay, no new
  sequence number. Different content = `IDENTICAL_RECORD_CONFLICT`.
* `grant` / `revoke(subject, purpose, op_id=None)` — idempotent when it
  repeats the currently effective state, or when the same stable `op_id`
  repeats. Conflicting `op_id` reuse = `AUTHORIZATION_CONFLICT`.
* All accepted data and authorisation operations share one contiguous prefix
  (`service_seq`) and one global journal (`commit_log.seq`). Rejected
  operations roll back and consume neither.
* `view(k, purpose)` — at prefix `k`, each subject's latest data revision
  with `service_seq <= k` if non-retracted, filtered by that subject's latest
  accepted authorisation action for `purpose` within the prefix. A later
  grant cannot revive a retracted row.

## Publisher semantics

* `draft(request_id, k, purpose, K, D, weights)` freezes the exact private
  prefix-`k` slice (identities + revisions), exhaustively enumerates all level
  vectors, and stores the minimum-loss vector (ties by lexicographically
  smallest `L`), per-group counts and the public rows.
* `loss(L) = Σ L_col · weight_col · n_rows`. Column heights are ignored here;
  if domains of unequal height are used, see the note in `docs/DESIGN.md`.
* Empty slice → `L=(0,0,0)`, loss 0, empty public rows. If even `(*,*,*)`
  cannot satisfy K/D per group, the draft is `state="infeasible"` with a full
  `infeasible_report` (group failures at the top vector are conclusive because
  generalisation only merges groups). It cannot be approved.
* Group key is the generalised `(age,district,job)`; **sensitive is never
  generalised**. A group needs ≥ K rows and ≥ D distinct raw sensitive values;
  repeated identical sensitive values count once for D and every row counts
  for K.
* Public rows are sorted by the generalised triple then sensitive, one row per
  allowed input row — no deletion, duplication or sampling.
* `review(draft_id, decision, reason)` — first human decision is immutable;
  a replay with the same decision+reason is idempotent. Rejection touches
  neither data nor consent.
* `release(draft_id, release_id)` — only approved drafts, and only if at
  release time the **current** authorised slice has the identical frozen
  identity/version set. The check and the release insert share one
  `BEGIN IMMEDIATE` transaction, so a race is decided by whichever of
  release/revoke commits first. Success writes an immutable public file and a
  private source ledger and records a persistent `release_seq`.
* An old draft remains fully inspectable (`inspect_draft`) for private audit,
  but `still_releasable` is false once the current slice changed. The same
  old `k` stays legal for a **new** draft and reproduces identical bytes.
* Reopen: all history, human decisions, releases and files survive; nothing is
  held only in memory.

## Deliberate limits

No differential privacy, probabilistic attack model, arbitrary-hierarchy
inference, real identity system, or crash-kill recovery beyond SQLite
durability. Enumeration is exhaustive: the task caps rows at 60 and levels at
3 per column, so at most 4³ = 64 vectors per draft.
