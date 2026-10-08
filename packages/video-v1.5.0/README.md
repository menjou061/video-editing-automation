# Video automation candidate v1.5.0

Production deployment target: a Windows rendering machine. The package worker
and production queue are not deployed on macOS or Linux. macOS is used only as a
separate human-review endpoint when the review handoff is configured; Linux is
unsupported.

Default Eval mode is observation. Visual policy remains independently versioned
at 1.4.1. Do not deploy over a running queue or publish release tags before the
two-real-task rollout gate passes. See ../../docs/video-eval-v1.5.0.md.

This is the reusable video-automation capability extracted from the latest
local video package. It contains the matcher, timing contract, Jianying draft
writer, closed-loop gates, and regression tests.

The package intentionally excludes media, credentials, production logs,
machine-specific paths, and local knowledge-base content. Runtime deployment
must inject the environment variables documented in `ENVIRONMENT.md`.

The visual governance policy is a separately deployable business layer. This
package consumes its version and evidence contract; it does not copy business
rules into Diaodu or RDM.
