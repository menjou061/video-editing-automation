#!/usr/bin/env python3
"""Doubao Skill launcher for the packaged orchestration CLI.

The release package keeps the launcher beside the ``orchestrator`` package so
the Windows task template and the published ``entrypoint`` resolve to the
same executable surface.
"""
from __future__ import annotations

import os
import sys


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from orchestrator.cli import main

    raise SystemExit(main(sys.argv[1:]))
