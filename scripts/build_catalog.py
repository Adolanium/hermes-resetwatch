"""Build the catalog package from the standalone Desktop files.

Run with --check in CI to reject stale packaged files. Catalog packages use
Hermes updates so their Desktop copy cannot bypass a reviewed commit pin.
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build(check=False):
    config = json.loads((ROOT / "catalog-package.json").read_text())
    name = config["name"]
    message = f"This package uses Hermes updates. Run hermes plugins update {name}, then rescan Desktop plugins."
    source = (ROOT / "plugin.js").read_text(encoding="utf-8")
    mode = config["updater"]
    if mode == "shared":
        start = "  async function run(action = 'check') {"
        end = "  function register(ctx) {"
        replacement = "  async function run() {\n    patch({ open: true, busy: false, offer: null, error: '', message: " + json.dumps(message) + " });\n  }\n"
    elif mode == "ssh":
        start = 'async function runUpdate(action = "check") {'
        end = 'const ROUTE = "/ssh-connections";'
        replacement = "async function runUpdate() {\n  updatePatch({ busy: false, available: null, restoreAvailable: null, error: '', message: " + json.dumps(message) + " });\n}\n"
    elif mode != "none":
        raise ValueError("Unknown updater mode: " + mode)
    if mode != "none":
        if source.count(start) != 1 or source.count(end) != 1:
            raise ValueError("Updater structure changed; review catalog update handling before releasing")
        first, last = source.index(start), source.index(end)
        if last <= first:
            raise ValueError("Unexpected updater function order")
        source = source[:first] + replacement + source[last:]

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
        outputs["catalog/desktop/" + companion] = (ROOT / companion).read_text(encoding="utf-8")
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
