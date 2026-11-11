"""Pin public Google Discovery schemas; never fetch discovery at runtime.

Run from this repository: python scripts/build_workspace_catalog.py
The Agent receives the exact same catalog and validation implementation.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

APIS = {
    "gmail": ("gmail", "v1"), "calendar": ("calendar", "v3"),
    "drive": ("drive", "v3"), "docs": ("docs", "v1"),
    "sheets": ("sheets", "v4"), "slides": ("slides", "v1"),
    "tasks": ("tasks", "v1"), "contacts": ("people", "v1"),
    "chat": ("chat", "v1"), "meet": ("meet", "v2"),
}


def fetch(item):
    service, (api, version) = item
    url = f"https://www.googleapis.com/discovery/v1/apis/{api}/{version}/rest"
    try:
        with urlopen(url, timeout=45) as response:
            document = json.load(response)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        url = f"https://{api}.googleapis.com/$discovery/rest?version={version}"
        with urlopen(url, timeout=45) as response:
            document = json.load(response)
    methods = {}

    def walk(resource, prefix=""):
        for name, method in resource.get("methods", {}).items():
            key = prefix + name
            scopes = [s for s in method.get("scopes", []) if not any(
                term in s for term in (".admin", "chat.app.", "chat.bot", "chat.import", "chat.memberships.app", "cloud-platform", "directory.")
            )]
            if not scopes or ".settings." in key or key.startswith("users.settings."):
                continue
            if "administrator" in method.get("description", "").lower() and not scopes:
                continue
            kind = "read" if method["httpMethod"] == "GET" or name in {
                "batchGet", "batchGetByDataFilter", "search", "searchContacts", "searchDirectoryPeople"
            } or key == "freebusy.query" else "write"
            record = {k: method[k] for k in (
                "path", "httpMethod", "parameters", "request", "response", "supportsMediaDownload", "mediaUpload"
            ) if k in method}
            record.update(kind=kind, scopes=scopes, description=method.get("description", "")[:1200])
            methods[key] = record
        for name, child in resource.get("resources", {}).items():
            walk(child, prefix + name + ".")

    walk(document)
    return service, {
        "source": url, "revision": document.get("revision"),
        "baseUrl": document["rootUrl"] + document.get("servicePath", ""),
        "rootUrl": document["rootUrl"], "methods": methods, "schemas": document.get("schemas", {}),
    }


def main():
    root = Path(__file__).resolve().parents[1]
    with ThreadPoolExecutor(max_workers=5) as pool:
        data = dict(pool.map(fetch, APIS.items()))
    target = root / "app/workspace/catalog.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    agent = root.parent / "Module AI - Agent/app/workspace"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "catalog.json").write_bytes(target.read_bytes())
    validator = root / "app/workspace/catalog.py"
    if validator.exists():
        (agent / "catalog.py").write_bytes(validator.read_bytes())
    (agent / "__init__.py").touch()
    for service, spec in data.items():
        print(f"{service}: {len(spec['methods'])} methods, revision {spec['revision']}")


if __name__ == "__main__":
    main()
