"""Build the catalog package from the standalone Desktop files.

Run with --check in CI to reject stale packaged files. Catalog packages use
Hermes updates so their Desktop copy cannot bypass a reviewed commit pin.
"""
import argparse
import json
from pathlib import Path
from catalog_policy import strip_updater
from catalog_probe import readonly_probe

ROOT = Path(__file__).resolve().parent.parent


def build(check=False):
    config = json.loads((ROOT / "catalog-package.json").read_text())
    name = config["name"]
    source = strip_updater((ROOT / "plugin.js").read_text(encoding="utf-8"), config["updater"])

    manifest = {
        "name": name, "version": config["version"],
        "description": config["description"], "author": "Adolanium",
        "manifest_version": 1, "kind": "standalone",
        "provides_tools": [], "provides_hooks": [],
        "provides_middleware": [], "requires_env": [],
    }
    # JSON is valid YAML and keeps this build dependency-free.
    outputs = {
        "catalog/plugin.yaml": json.dumps(manifest, indent=2) + "\n",
        "catalog/__init__.py": '"""Desktop package. Electron loads desktop/plugin.js."""\n\n\ndef register(ctx):\n    """No Agent tools or hooks; enable the Desktop component in Capabilities."""\n',
        "catalog/desktop/plugin.js": source,
    }
    for companion in config["companions"]:
        outputs["catalog/desktop/" + companion] = readonly_probe((ROOT / companion).read_text(encoding="utf-8"))
    stale = []
    for name, content in outputs.items():
        target = ROOT / name
        if check:
            if not target.is_file() or target.read_text(encoding="utf-8") != content:
                stale.append(name)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
    if stale:
        raise SystemExit("Run python scripts/build_catalog.py; stale files: " + ", ".join(stale))
    print("Catalog package verified" if check else "Catalog package built")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    build(parser.parse_args().check)
