---
name: jianying-editor
description: Create and inspect editable Jianying drafts through the bundled JyProject wrapper, including media, audio, captions, and safe Windows setup; route end-to-end manifest builds to doubao-jianying-orchestrator.
---

# Jianying Editor

Use the bundled `scripts/jy_wrapper.py` and `pyJianYingDraft` vendor package for editable draft creation. Keep media references valid and use integer microseconds for timeline values.

## Quality rules

- Preserve source media and write to a new draft. Do not close, switch, or overwrite a draft while Jianying is running.
- Keep audio inside its matching video/full-timeline boundary; never leave trailing narration after the picture ends.
- Validate the saved draft independently before reporting success.
- Prefer cached metadata and generated assets; do not repeat expensive probes or TTS synthesis unnecessarily.
- For end-to-end generation, always hand the manifest to the sibling
  `doubao-jianying-orchestrator` and run `run.py build`. This skill supplies
  the writer API; it is not a fallback writer for agent-created JSON.
- `byted-mediakit` may assist with understanding or audio processing, but must
  not create, install, copy, or patch JianYing draft JSON.
- Never create task-local `gen_*.py` or `install_*.py` draft scripts and never
  manually edit `draft_content.json`, `draft_info.json`, or
  `draft_meta_info.json`.

## Subtitle defaults (v1.3.9)

- All subtitle entry points (`add_text_simple`, `add_narrated_subtitles`, and
  SRT import) default to size `8`, bold, and the existing configured
  color.
- Displayed subtitle text removes Chinese and English punctuation while
  retaining letters, numbers, and necessary spaces.
- Subtitle clip position defaults to `X=0`, `Y=-0.8` in JianYing's half-canvas-
  height coordinate system. A caller may override these values explicitly; the
  orchestrator's generated drafts use the defaults and validate them after
  saving.

For the end-to-end manifest workflow, use the sibling `doubao-jianying-orchestrator` skill. Read `rules/` or `references/` only for the specific advanced editor operation being requested.

When reviewing a generated draft, require a separate BGM audio track when BGM
was selected, a narration track for generated/file voiceover, and a subtitle
track whose materials use structured `content` JSON with a non-empty text
payload. A JSON file that merely contains `type: title`, `text`, and
`text_style` is not an accepted subtitle implementation.
