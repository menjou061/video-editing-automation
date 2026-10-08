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
