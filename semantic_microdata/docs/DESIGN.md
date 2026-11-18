# Design notes and trade-offs

This document explains the choices behind the implementation, the exact
contract as encoded, and the trade-offs the task asked to make explicit.

## 1. One global order, two meanings of "prefix"

Two sequences coexist:

* `commit_log.seq` — the **global** journal order shared by data operations,
  authorisation operations, drafts, reviews and releases. This is the order
  used to decide a concurrent release/revoke race.
* `operations.service_seq` — a contiguous prefix over accepted **data and
  authorisation** operations only. `view(k, purpose)` reads exactly this
  prefix.

If drafts/reviews/releases shared the `k` counter, a caller's `k` from "before
any draft" would be perturbed by publisher-only activity. Keeping the service
prefix separate means:

* `k` is exactly "the first k accepted data/consent operations", as the task
  describes;
* replaying a draft does not shift any subsequent `k`;
* release-time revalidation compares **global** live state against the
  **frozen** slice — it does not trust `k`.

The one global order still exists in `commit_log`, and a release transaction
inserts its own journal row inside the same transaction as the revalidation,
so the SQLite write lock gives the required commit ordering.

## 2. Do retracted rows disappear from views?

Yes: a retraction is a data operation with `row=None`. The view selects each
subject's highest revision ≤ k, and drops it if `retracted=1`. Crucially:

* the original row remains in `operations` forever, so any earlier prefix k
  still reproduces it;
* a later grant only changes the consent predicate; it does not undo the
  retraction (`retracted` is checked independently of consent).

This is what lets scenario 7 show: old k=8 → four rows, feasible at L=0;
current k=9 → three rows, feasible only at L=(1,0,0); both are accurate; only
the current draft may be released.

## 3. Replay vs conflict

Operations carry content-derived hashes. The identities are:

* data: `(kind, record_id, subject_id, revision, retracted, row)`
* auth: `(kind, subject_id, purpose, action)`

For auth, **repeating the currently effective state** is a replay, not a new
operation. This is why `revoke` twice is a replay. It also means grant after a
revoke is a *new* accepted operation (the state changed twice), which is
needed for prefix views.

For data, an exact replay of an accepted `(record_id, revision)` returns the
original sequence number. A later low revision is a distinct case and returns
`STALE_REVISION` rather than a replay: the highest-revision rule is
monotonic, not "first writer wins".

## 4. Why full generalisation is a feasibility witness

For any level vector L and any L' ≥ L componentwise, each group under L' is a
union of groups under L (parents never split groups; `Hierarchy` validates
this when the domains load). Thus:

* if a group under L' is smaller than K / has fewer than D distinct sensitive
  values, the same is true of the sub-groups under L;
* therefore if the top vector `(*,*,*)` is infeasible, **every** vector is
  infeasible.

`infeasibility_report` returns the top-vector group failures and states that
they are conclusive. It never silently drops a failing group.

## 5. Cost of exhaustive enumeration

With the task bounds (≤ 60 rows, ≤ 3 generalisation steps per column), there
are at most 4×4×4 = 64 vectors. Each vector is O(n_rows) to group. So a draft
costs at most a few thousand row visits — much cheaper than building an index
or a lattice. The implementation therefore always enumerates afresh:

* the optimizer is a pure function, easy to test and audit;
* the independent verifier re-derives it from the ancestor tables without
  importing optimizer code;
* no cache can go stale between a consent change and a new draft.

If the row cap were raised, the natural next step is caching group counts per
vector and merging adjacent levels; the prefix/consent plumbing does not
change.

## 6. Release-time revalidation, atomically

The release transaction:

1. `BEGIN IMMEDIATE` (blocks a concurrent writer);
2. reads `service_head` and the current authorised view;
3. recomputes the frozen identity digest (subject, record, revision, op id,
   values) and compares it to the stored digest;
4. per-source checks for clear error reasons (`CONSENT_MISSING` vs
   `STALE_DRAFT`);
5. allocates a journal slot and a `release_seq`, writes public + private files
   and the release row, then commits.

Because step 1 takes the write lock, a concurrent revoke either commits before
(and step 3 fails) or waits until after (and the immutable file stands). The
two race scenarios demonstrate both orders. The code does not rely on
notifications or on the client retrying.

## 7. Immutable old files

The public file is written once with the release's content and its SHA-256 is
recorded. A later retraction/revision does not rewrite it. The release row and
private ledger remain queryable, and `inspect_draft` distinguishes:

* **reproducible private result** (frozen slice + selection) — always true;
* **current releasability** — true only if the live authorised identity set
  still matches.

The old k is still legal for a new draft; the resulting bytes match the old
ones because the exact slice is unchanged. This directly exercises the
task's requirement: "requesting old k for private verification is legal and
its result is unchanged; misreading old view when consuming a new k is the
error".

## 8. Generalisation loss

The implementation uses the literal contract:
`loss(L) = L_age*w_age*n + L_district*w_district*n + L_job*w_job*n`.

If material columns have different heights, level `i` is not comparable
across columns. A normalised variant
`L_i / max_level_i * w_i * n` is available as `optimizer.normalised_loss`
(integer arithmetic via the LCM of column heights). The default selection
path deliberately uses the literal formula; with equal heights (as in the
supplied material) the two agree.

## 9. Public / private separation

* **Public file**: `release_id`, `draft_id`, `release_seq`, `k`, `purpose`,
  K, D, weights, levels, loss, row_count, and `rows[]` with only the four
  published columns.
* **Private ledger**: the same metadata plus `frozen_slice` with
  `subject_id`, `record_id`, `revision`, `version_commit_seq`,
  `version_commit_order`, `consent_commit_seq` and values.

`verify/reference_check.py` asserts no identity string, `revision`,
`commit_seq` or `version_op_id` appears in the public JSON, and that the
public file's SHA-256 matches the recorded digest.
