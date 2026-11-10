"""Verify that the portal template matches the Bicep build, ignoring compiler metadata."""

import json
import sys
from pathlib import Path


def without_generator(value):
    if isinstance(value, dict):
        return {
            key: without_generator(item)
            for key, item in value.items()
            if key != "_generator"
        }
    if isinstance(value, list):
        return [without_generator(item) for item in value]
    return value


def check(compiled: Path) -> int:
    committed = Path(__file__).resolve().parents[1] / "infra" / "azuredeploy.json"
    if without_generator(json.loads(compiled.read_text(encoding="utf-8-sig"))) != (
        without_generator(json.loads(committed.read_text(encoding="utf-8-sig")))
    ):
        print("Portal template is stale. Run: az bicep build --file infra/main.bicep --outfile infra/azuredeploy.json")
        return 1
    print("Portal template matches the Bicep build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(check(Path(sys.argv[1])))
