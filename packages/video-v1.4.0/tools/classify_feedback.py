#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-file", required=True)
    ap.add_argument("--output-file", required=True)
    ap.add_argument("--skill-root", required=True)
    args = ap.parse_args()
    sys.path.insert(0, str(Path(args.skill_root)))
    from orchestrator.feedback_policy import classify_feedback
    text = Path(args.input_file).read_text(encoding="utf-8-sig")
    result = classify_feedback(text)
    Path(args.output_file).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
