"""Thin Python wrapper around the Hardhat Layer 3 deployment script."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    command = [
        "npx",
        "hardhat",
        "run",
        "contracts/scripts/deploy.js",
        "--network",
        (sys.argv[1] if len(sys.argv) > 1 else "polygonMumbai"),
    ]
    completed = subprocess.run(command, cwd=repo_root, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
