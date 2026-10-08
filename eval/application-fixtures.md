# Synthetic application fixture candidate v1

These **60 English cases were prepared before any model run or tuning**. They
are a leader-approved immutable input freeze, not a completed S4
or hiring-quality gate. `applications-manifest.json` records exact bytes and
split membership; `test_application_fixtures.py` independently pins the SHA-256.
Approve this version by recording the hash in the leader's evidence, not by
silently rewriting the dataset or treating the manifest as an authority grant.

## Composition and boundaries

- Exactly 30 tech and 30 finance cases; 15 tune and 15 held-out in each sector.
- Each sector/split has eight direct evidence cases, four missing-fact cases and
  three adversarial cases. Total: 30 tune, 30 held-out, 24 unanswerable, 12
  adversarial; held-out has 12 unanswerable and six adversarial.
- Adversarial categories: lexical/negation distractors, conflicting equally
  authoritative facts, and impressive metrics present only as pending proposals.
- Stable UUIDv5 IDs isolate every case, profile, application and source record.
  Load each case separately: combining source banks across owners would destroy
  the intended isolation contract. Pending proposals are not confirmed facts.
- All personal facts and employer excerpts are authored synthetic material.
  Names refer to fictional student exercises, not real applicants or employers;
  employer excerpts are **not fetched sources or verified public citations**.

A case contains its exact question, retrieval query, confirmed fact bank,
separate pending proposals, and canonical synthetic employer excerpts. `gold`
separates **personal support**, **employer relevance**, and **retrieval relevance**.
For the contradiction cases both quotes are retrieval-relevant, but neither
supports a resolved numerical answer. `answerable=false` and empty support IDs
mean request confirmation, not generate a plausible guess. Matching UUIDs tests
fixture integrity; a future evaluator must still assess meaning and exact spans.

## Label basis and ambiguity handling

Direct cases ask only about explicitly recorded actions. They do not ask for
employment, impact numbers, real client deals or unrelated qualifications.
Missing-fact cases explicitly lack a needed eligibility, date, credential,
employment or quantitative record. A student team is not employee management;
a synthetic finance model is not a real portfolio return or executed deal.
Conflicting team counts have no authority or recency tie-breaker, so unresolved
support is intentional. Pending metric cases lack confirmation and independent
measurements. The per-case `label_basis` explains each authored decision.

The v2 `software_not_snakes` query, relevance keys and **all ten original fact
records** from `cases.json` are copied unchanged into one tech tune case. The
actual v2 record ranked wildlife above software in all retrieval branches;
retaining this input does not assert that the regression has been fixed.
The original `cases.json` and saved v2 results are not modified.

## Required next evidence; no inferred passes

1. Leader approved the exact byte hash and fixed split after an independent
   synthetic-integrity audit. No runtime quality result was inferred.
2. Implement question-specific evaluation against the real retrieval pipeline;
   tune only on `tune`, run held-out once for a qualification attempt, retain raw
   outputs and disclose failures rather than relabelling them.
3. Independently audit the ground truth. Authored labels are **not** 40
   human-audited claim judgements, nor two independent editorial reviewers.
4. Measure retrieval, unsupported-claim rejection, abstention, privacy isolation
   and grounded drafting separately. Existing `run.metrics` can score retrieval
   when relevant IDs exist; it deliberately returns `None` for missing relevance,
   so it cannot measure semantic abstention.
5. Supply the separate 12-answer blind tech/finance editorial comparison and
   actual human reviews; these fixtures do not establish application quality.

Tune and held-out reuse related synthetic capability templates, although their
record IDs and project texts are isolated. This split can detect regressions but
is **not strong evidence of generalisation to real people, diverse companies or
real hiring outcomes**. Later independently authored/consented evaluation should
be separately versioned. Never tune prompts or thresholds against held-out cases,
and never alter this frozen version to conceal a failing model run.

## Offline integrity checks

From repository root, using the existing environment (no downloads or models):

```sh
PYTHONPATH=backend uv run --no-sync --project backend \
  python -m unittest discover -s eval -p 'test_*.py'
```

The eight fixture tests and three existing metric tests verify labels, counts,
isolation and frozen input integrity only—not runtime retrieval quality.

## Independent integrity audit limitations

The audit found 19 of 30 held-out question strings also in the tune split, and
all 36 answerable cases place support first in their confirmed-fact banks. Gold,
category, answerability, split and source keys are evaluation metadata, never
model context. A runner must use deterministic presentation randomisation without
rewriting the frozen dataset. These cases do not test malicious instructions;
source-injection fixtures remain a separate mandatory gate. Fixed hashes preserve
inputs and split membership but do not enforce tuning-access restrictions.
