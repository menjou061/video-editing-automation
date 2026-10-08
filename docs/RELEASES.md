# Release archive notes

## 1.4.0

- Source: `packages/video-v1.4.0/`, extracted from the immutable
  `video-v1.4.0` source tag.
- The former private repository had the tag but no binary release asset. The
  downloadable ZIP was rebuilt from that tagged source; it is not represented
  as an original historical ZIP.
- Source manifest: 107 entries; all package checksum entries validated.
- Tests: 9 package tests and 158 orchestrator tests passed.
- ZIP SHA-256: `698a5668ffd234faf1f479da87afd85906c8af231f0680124d7036bfccd77df1`.

## 1.5.0

- Original candidate source: `packages/video-v1.5.0/`, extracted byte-for-byte
  from the original candidate ZIP at the time of that archive.
- Production deployment target: Windows rendering machine. macOS is only a
  separate human-review endpoint; Linux is unsupported.
- Original candidate manifest: 120 entries; original ZIP SHA-256:
  `28cdb3884f52db2f1c8a8bce03a7dd97a64b7bd07fead3cf79320299be8ba961`.
- Windows deployment clarification is published as candidate refresh
  `video-v1.5.0-winrc2`; it keeps component version 1.5.0 and visual policy
  1.4.1, and preserves the original archive unchanged.
- Refreshed package manifest: 120 entries; all package checksum entries
  validated. Tests: 24 package tests and 197 orchestrator tests passed.
- Refreshed ZIP SHA-256 is listed in `releases/SHA256SUMS-video-v1.5.0-winrc2.txt`.
- Both artifacts remain candidates. Production observation and human
  acceptance gates remain separate from source/package verification.

Checksum files are stored under `releases/` and attached to the matching
GitHub Release alongside each ZIP.

## 1.5.0 compatibility source revision (2026-10-08)

- The current `packages/video-v1.5.0/` source contains the reviewed Windows
  configuration, 1.4.1 compatibility, guarded timing, and isolated legacy-state
  migration changes. Component versions remain 1.5.0; visual policy remains
  1.4.1. See the [compatibility assessment](../packages/video-v1.5.0/COMPATIBILITY_REVIEW.md).
- Source validation including the bundled QC follow-up: 40 package tests and
  227 orchestrator tests passed,
  including a repeat run from an independently extracted candidate ZIP.
  The source manifest and checksum file cover this source revision.
- This source PR does not replace the original or `video-v1.5.0-winrc2` release
  assets. The 120-entry counts and ZIP checksums above describe those historical
  artifacts, not the newer source tree.
- The existing project QC helper is now included in the source package and
  uses the packaged vision interface. FFmpeg extraction of synthetic media
  passed; its model responses were simulated. Normal deployment does not
  need a separate QC script download.
- Windows PowerShell, scheduler-account DPAPI, the configured vision service,
  and two real task observations still require deployment acceptance. Candidate
  source validation does not assert that the renderer has been upgraded.
