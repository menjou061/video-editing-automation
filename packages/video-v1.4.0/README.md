# Video automation release v1.4.0

This is the reusable video-automation capability extracted from the latest
local video package. It contains the matcher, timing contract, Jianying draft
writer, closed-loop gates, and regression tests.

The package intentionally excludes media, credentials, production logs,
machine-specific paths, and local knowledge-base content. Runtime deployment
must inject the environment variables documented in `ENVIRONMENT.md`.

The visual governance policy is a separately deployable business layer. This
package consumes its version and evidence contract; it does not copy business
rules into Diaodu or RDM.
