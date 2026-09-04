# Phase 2 benchmark authoring scaffold

`manifest.draft.json` is intentionally empty. It proves the manifest contract
can be loaded, but it is **not** benchmark evidence and must not be reported as
the 150-case dataset.

The Python source of truth is `analytics_agent.benchmark.BenchmarkManifest`.
It supports two states:

- `draft`: may be empty or incomplete while cases are authored and reviewed.
- `frozen`: requires the pre-registered train/dev/test split of `60/30/60`, the
  generate/clarify/reject totals of `108/21/21` (including the complete cell
  distribution), and verified content hashes for every split.

Every case has a stable ID, split, disposition, family, paraphrase group and
gold contract. Generate cases carry logical-plan/binding/output/policy gold;
clarify and reject cases carry a structured decision gold. Only train cases may
set `retrieval_eligible: true`.

Before freezing, run the contamination scanner. Repeated request hashes,
normalized text or paraphrase groups across splits are hard failures. Repeated
entity sets are recorded for review but are not failures because the same
symbols may intentionally appear in train, dev and test.

The existing `../ground_truth.json` remains the separate Phase 1 8-case
regression and must not be copied into this benchmark.
