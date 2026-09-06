from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage allow-listed GameFlow resources")
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("--manifest", default="config/resource_manifest.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    source = Path(args.source).resolve()
    destination = Path(args.destination).resolve()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    excluded_dirs = {str(value).casefold()
                     for value in manifest.get("exclude_directories", [])}
    excluded_suffixes = {str(value).casefold()
                         for value in manifest.get("exclude_suffixes", [])}

    def ignored(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names
                if name.casefold() in excluded_dirs
                or Path(name).suffix.casefold() in excluded_suffixes}

    missing: list[str] = []
    for item in manifest.get("includes", []):
        relative = Path(str(item["source"]))
        origin = source / relative
        if not origin.is_dir():
            missing.append(str(origin))
            continue
        for required in item.get("required", []):
            path = origin / str(required)
            if not path.is_file():
                missing.append(str(path))
        if not args.dry_run:
            shutil.copytree(origin, destination / relative,
                            dirs_exist_ok=True, ignore=ignored)
    if missing:
        print("\n".join(f"缺少清单资源：{value}" for value in missing))
        return 1
    if not args.dry_run:
        for relative in manifest.get("writable_directories", []):
            (destination / str(relative)).mkdir(parents=True, exist_ok=True)
    print(f"资源清单验证通过：{len(manifest.get('includes', []))} 个目录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
