"""Pinned Google contracts shared byte-for-byte with the Agent."""
import copy
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from jsonschema import Draft7Validator, ValidationError

SERVICES = ("gmail", "calendar", "drive", "docs", "sheets", "slides", "tasks", "contacts", "chat", "meet")
MAX_PAYLOAD = 1024 * 1024


@lru_cache(maxsize=1)
def catalog():
    return json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))


def methods_for(service):
    if service not in SERVICES:
        raise ValueError("Unknown Workspace service")
    return catalog()[service]["methods"]


def operation(service, method):
    result = methods_for(service).get(method)
    if result is None:
        raise ValueError(f"Unsupported Workspace method: {service}.{method}")
    return result


def is_authorized(service, method, scopes):
    return bool(set(operation(service, method)["scopes"]) & set(scopes))


def permissions_for(scopes):
    return sorted({f"workspace.{service}.{spec['kind']}" for service in SERVICES
                   for spec in methods_for(service).values() if set(spec["scopes"]) & set(scopes)})


def oauth_scopes(services=None):
    selected = list(SERVICES if services is None else services)
    if not selected or any(name not in SERVICES for name in selected):
        raise ValueError("Unknown or empty Workspace services selection")
    return sorted({scope for service in selected for spec in methods_for(service).values() for scope in spec["scopes"]})


def _schema(value):
    if isinstance(value, list):
        return [_schema(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {k: _schema(v) for k, v in value.items() if k not in {"id", "location", "required", "annotations"}}
    if isinstance(value.get("required"), list):
        out["required"] = value["required"]
    if "$ref" in out:
        out["$ref"] = "#/definitions/" + out["$ref"]
    if out.get("type") == "any":
        out.pop("type")
    for bound in ("minimum", "maximum"):
        if isinstance(out.get(bound), str):
            out[bound] = float(out[bound])
    if value.get("repeated"):
        out.pop("repeated", None)
        return {"type": "array", "items": out}
    if out.get("type") == "object" and "additionalProperties" not in out:
        out["additionalProperties"] = False
    return out


@lru_cache(maxsize=10)
def definitions_for(service):
    return {key: _schema(value) for key, value in catalog()[service]["schemas"].items()}


@lru_cache(maxsize=1024)
def request_schema(service, method):
    spec = operation(service, method)
    parameters = copy.deepcopy(spec.get("parameters", {}))
    parameters.update({"fields": {"type": "string"}, "alt": {"type": "string", "enum": ["json", "media"]}})
    props = {key: _schema(value) for key, value in parameters.items()}
    required = [key for key, value in parameters.items() if value.get("required")]
    body = _schema(spec.get("request", {"type": "object", "additionalProperties": False}))
    return {"type": "object", "additionalProperties": False,
            "properties": {"parameters": {"type": "object", "properties": props, "required": required, "additionalProperties": False}, "body": body},
            "required": ["parameters", "body"],
            "definitions": definitions_for(service)}


def validate_call(service, kind, method, parameters=None, body=None):
    spec = operation(service, method)
    if spec["kind"] != kind:
        raise ValueError(f"Method is not a {kind} operation")
    params, payload = copy.deepcopy(parameters or {}), copy.deepcopy(body or {})
    if not isinstance(params, dict) or not isinstance(payload, dict):
        raise ValueError("parameters and body must be JSON objects")
    # Gmail MIME has base64 inside base64; its bounded media allowance is
    # separate from ordinary JSON method payloads. Schema validation still runs.
    mime_method = service == 'gmail' and method in {'users.messages.send', 'users.drafts.create', 'users.drafts.update'}
    limit = 12 * 1024 * 1024 if mime_method else MAX_PAYLOAD
    if len(json.dumps([params, payload], ensure_ascii=False).encode()) > limit:
        raise ValueError(f"Workspace request exceeds {limit // (1024 * 1024)} MiB")
    if service == "gmail":
        if params.get("userId", "me") != "me":
            raise ValueError("Gmail userId must be me")
        if "userId" in spec.get("parameters", {}):
            params["userId"] = "me"
    if kind == "read" and spec["httpMethod"] == "GET" and payload:
        raise ValueError("GET reads cannot contain body")
    for key in ("pageSize", "maxResults"):
        if key in spec.get("parameters", {}) and key not in params:
            params[key] = 20
        if key in params and (type(params[key]) is not int or not 1 <= params[key] <= 100):
            raise ValueError(f"{key} must be between 1 and 100")
    try:
        Draft7Validator(request_schema(service, method)).validate({"parameters": params, "body": payload})
    except (ValidationError, TypeError, ValueError) as exc:
        message = exc.message if isinstance(exc, ValidationError) else str(exc)
        raise ValueError(f"Invalid Workspace request: {message[:300]}") from exc
    for key, definition in spec.get("parameters", {}).items():
        if definition.get("location") == "path" and key in params:
            value = str(params[key])
            if key == "range" and service == "sheets":
                if not value or len(value) > 2000 or any(ord(char) < 32 for char in value):
                    raise ValueError("Invalid Sheets range")
                continue
            if not value or any(c in value for c in ("?", "#", "\\", "%", ":")) or any(p in {".", "..", ""} for p in value.split("/")):
                raise ValueError(f"Invalid resource path: {key}")
            reserved = "{+" + key + "}" in spec["path"]
            if not reserved and "/" in value:
                raise ValueError(f"Invalid resource identifier: {key}")
    return spec, params, payload


def build_url(service, spec, params, upload=False):
    route = spec["path"]
    base = catalog()[service]["baseUrl"]
    if upload:
        route = spec.get("mediaUpload", {}).get("protocols", {}).get("simple", {}).get("path")
        if not route:
            raise ValueError("Method does not support simple media upload")
        base = catalog()[service]["rootUrl"].rstrip("/") + "/"
        route = route.lstrip("/")
    def replace(match):
        reserved, name = match.groups()
        if name not in params:
            raise ValueError(f"Missing resource identifier: {name}")
        return quote(str(params[name]), safe="/" if reserved else "")
    return base + re.sub(r"\{(\+?)([^}]+)\}", replace, route)


def overview(service=None, method=None):
    if method:
        return {"service": service, "method": method, "operation": operation(service, method), "schema": request_schema(service, method)}
    result = {}
    for name in ([service] if service else SERVICES):
        result[name] = [{"method": key, "kind": spec["kind"], "description": spec["description"][:200]}
                        for key, spec in methods_for(name).items()]
    return result
