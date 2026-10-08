# Video automation candidate v1.5.0

Production deployment target: a Windows rendering machine. The package worker
and production queue are not deployed on macOS or Linux. macOS is used only as a
separate human-review endpoint when the review handoff is configured; Linux is
unsupported.

Default Eval mode is observation. Visual policy remains independently versioned
at 1.4.1. Do not deploy over a running queue or publish release tags before the
two-real-task rollout gate passes. See ../../docs/video-eval-v1.5.0.md.

The current source includes the 1.4.1 compatibility review and fixes. Read
[`COMPATIBILITY_REVIEW.md`](COMPATIBILITY_REVIEW.md) for the assessment,
validation evidence, and Windows rollout prerequisites. This source revision
is newer than the original and `video-v1.5.0-winrc2` downloadable archives;
those release assets are preserved and are not replaced by a source PR.

This is the reusable video-automation capability extracted from the latest
local video package. It contains the matcher, timing contract, Jianying draft
writer, closed-loop gates, and regression tests.

The package intentionally excludes media, credentials, production logs,
machine-specific paths, and local knowledge-base content. Runtime deployment
must inject the environment variables documented in `ENVIRONMENT.md`.

Before using a renderer, run `python tools/package_preflight.py --package-root .`
from Windows PowerShell. A missing `draft_visual_qc.py`, a mismatched package
identity, or a non-Windows host is a blocking result; offline package tests do
not certify renderer readiness. The QC helper must be supplied by the machine
deployment and can be located with `JY_DRAFT_QC_SCRIPT`.

## 1.4.1 state migration

The migration utility keeps the 1.4.1 source untouched and writes an isolated
`legacy-v1.4.1/` archive under a new destination. Start with a read-only
preview, then apply only to the new 1.5.0 data root:

```powershell
python tools/migrate_legacy_state.py inspect --source D:\VideoPipeline
python tools/migrate_legacy_state.py migrate --source D:\VideoPipeline --destination D:\VideoPipeline-v150-migration
```

Legacy `OK` records remain historical and are never upgraded into a 1.5.0
success receipt. Errors and partial runs require an explicit retry-or-skip
decision; deferred missing-material rows stay deferred without an attempt.
Unknown or active locks require a live-process check and are never cleared by
the migration tool. Repeating the same migration is idempotent; a destination
containing a different snapshot is rejected.

The visual governance policy is a separately deployable business layer. This
package consumes its version and evidence contract; it does not copy business
rules into Diaodu or RDM.
