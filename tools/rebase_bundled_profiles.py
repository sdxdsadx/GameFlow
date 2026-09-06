from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameflow.config import load_config
from gameflow.settings import sync_bundled_tool_profiles


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebase bundled profiles to a release root")
    parser.add_argument("root")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    config = load_config(root / "config" / "workflow.json")
    sync_bundled_tool_profiles(root, config)
    print(f"内置工具配置已重定位：{root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
