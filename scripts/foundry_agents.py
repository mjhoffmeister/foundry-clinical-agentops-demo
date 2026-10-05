"""Foundry hosted-agent release helper (REST, api-version v1).

Used by azd hooks, GitHub Actions and the demo scripts. Every command prints a
JSON object on stdout; with --github-output the same keys are appended to
$GITHUB_OUTPUT.

  identity        hosted agent instance identity principal id
  selector        version currently pinned on the production endpoint
  versions        list versions (newest first)
  create-version  zip agent/ and create a new version reusing the definition of
                  a template version (defaults to the pinned version); waits
                  until the version is active. Does NOT move production traffic.
  pin             pin the production endpoint to a version, with optional
                  compare-and-swap (--expect-current)
  invoke          call a specific version with a fresh conversation

Auth: DefaultAzureCredential (az login locally, OIDC/WIF in Actions).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import sys
import time
import zipfile
from pathlib import Path

import requests
from azure.core.credentials import TokenCredential
from azure.identity import (
    AzureCliCredential,
    AzureDeveloperCliCredential,
    ChainedTokenCredential,
    DefaultAzureCredential,
)

API = "v1"
SCOPE = "https://ai.azure.com/.default"
ROOT = Path(__file__).resolve().parent.parent
EXCLUDE_DIRS = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".env"}
EXCLUDE_FILES = {".env", "uv.lock"}

_credential: TokenCredential | None = None


class FoundryError(RuntimeError):
    pass


def make_credential() -> TokenCredential:
    """Developer/CI credential pinned to AZURE_TENANT_ID.

    DefaultAzureCredential's Azure CLI leg uses whatever subscription/tenant is
    the CLI default, which silently breaks when someone switches `az account`.
    Pin the CLI/azd legs to the demo tenant; fall back to the default chain
    (managed identity, workload identity, ...) on hosted compute.
    """
    tenant = os.environ.get("AZURE_TENANT_ID", "").strip()
    hosted = os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
    if tenant and not hosted:
        return ChainedTokenCredential(
            AzureCliCredential(tenant_id=tenant),
            AzureDeveloperCliCredential(tenant_id=tenant),
            DefaultAzureCredential(),
        )
    return DefaultAzureCredential()


def _token() -> str:
    global _credential
    if _credential is None:
        _credential = make_credential()
    return _credential.get_token(SCOPE).token


def project_endpoint() -> str:
    ep = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip().rstrip("/")
    if not ep:
        raise FoundryError("FOUNDRY_PROJECT_ENDPOINT is not set")
    return ep


def _request(method: str, path: str, *, params: dict | None = None, retries: int = 4,
             retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504), **kwargs) -> requests.Response:
    url = f"{project_endpoint()}/{path}"
    params = {"api-version": API, **(params or {})}
    headers = kwargs.pop("headers", {})
    for attempt in range(retries + 1):
        headers["Authorization"] = f"Bearer {_token()}"
        try:
            resp = requests.request(method, url, params=params, headers=headers, timeout=300, **kwargs)
        except requests.ConnectionError:
            # Transient DNS/connection blips are common on long eval runs; retry like a 5xx.
            if attempt < retries:
                time.sleep(5 * (attempt + 1) + random.uniform(0, 5))
                continue
            raise
        if resp.status_code in retry_statuses and attempt < retries:
            time.sleep(5 * (attempt + 1) + random.uniform(0, 5))
            continue
        if resp.status_code >= 400:
            raise FoundryError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:2000]}")
        return resp
    raise FoundryError(f"{method} {path} exhausted retries")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def get_agent(name: str) -> dict:
    return _request("GET", f"agents/{name}").json()


def list_versions(name: str) -> list[dict]:
    out: list[dict] = []
    params: dict = {"limit": 100, "order": "desc"}
    while True:
        payload = _request("GET", f"agents/{name}/versions", params=params).json()
        items = payload.get("data") or payload.get("value") or []
        out.extend(items)
        if payload.get("has_more") and items:
            params["after"] = items[-1].get("id") or items[-1].get("version")
            continue
        return out


def get_version(name: str, version: str) -> dict:
    return _request("GET", f"agents/{name}/versions/{version}").json()


def pinned_version(name: str) -> str | None:
    agent = get_agent(name)
    rules = (((agent.get("agent_endpoint") or {}).get("version_selector") or {}).get("version_selection_rules")) or []
    pinned = [r for r in rules if int(r.get("traffic_percentage", 0)) == 100 and r.get("agent_version")]
    return str(pinned[0]["agent_version"]) if len(pinned) == 1 else None


def latest_version(name: str) -> str | None:
    agent = get_agent(name)
    latest = (agent.get("versions") or {}).get("latest") or {}
    return str(latest["version"]) if latest.get("version") else None


def identity_principal(name: str) -> str:
    agent = get_agent(name)
    candidates = [
        (agent.get("instance_identity") or {}).get("principal_id"),
        ((agent.get("versions") or {}).get("latest") or {}).get("instance_identity", {}).get("principal_id"),
    ]
    for c in candidates:
        if c:
            return c
    payload = requests.get(
        f"{project_endpoint()}/agents/{name}",
        params={"api-version": "2025-05-15-preview"},
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=60,
    ).json()
    pid = (payload.get("instance_identity") or {}).get("principal_id")
    if not pid:
        raise FoundryError(f"no instance identity found on agent {name}: {json.dumps(agent)[:1000]}")
    return pid


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
def zip_code(code_dir: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(code_dir.rglob("*")):
            rel = path.relative_to(code_dir)
            if any(part in EXCLUDE_DIRS for part in rel.parts) or path.name in EXCLUDE_FILES or path.is_dir():
                continue
            info = zipfile.ZipInfo(rel.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            # Explicit Unix mode: ZipInfo defaults to mode 000 on Linux, which makes the
            # remote build unable to read requirements.txt. Fixed values keep the hash OS-independent.
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            zf.writestr(info, path.read_bytes())
    return buf.getvalue()


def create_version(
    name: str, code_dir: Path, *, template_version: str | None, description: str, env_overrides: dict[str, str]
) -> dict:
    template_version = template_version or pinned_version(name) or latest_version(name)
    if not template_version:
        raise FoundryError("no template version found; deploy once with `azd deploy clinical-agent` first")
    definition = dict(get_version(name, template_version)["definition"])
    env = dict(definition.get("environment_variables") or {})
    env.update(env_overrides)
    definition["environment_variables"] = env
    for read_only in ("image", "image_digest", "code_zip_sha256", "code"):
        definition.pop(read_only, None)
    code = zip_code(code_dir)
    sha = hashlib.sha256(code).hexdigest()
    metadata = {"description": description[:500], "definition": definition}
    resp = _request(
        "POST",
        f"agents/{name}/versions",
        retries=6,
        # 409 = another version is being created concurrently (e.g. a PR run and a release)
        retry_statuses=(409, 429, 500, 502, 503, 504),
        headers={"x-ms-code-zip-sha256": sha, "Accept": "application/json"},
        files={
            "metadata": (None, json.dumps(metadata), "application/json"),
            "code": ("code.zip", code, "application/zip"),
        },
    )
    payload = resp.json()
    version = str(payload.get("version") or ((payload.get("versions") or {}).get("latest") or {}).get("version"))
    return {"version": version, "template_version": template_version, "code_sha256": sha}


def wait_active(name: str, version: str, timeout_s: int = 1500) -> str:
    deadline = time.time() + timeout_s
    status = "unknown"
    while time.time() < deadline:
        status = (get_version(name, version).get("status") or "unknown").lower()
        if status == "active":
            return status
        if status == "failed":
            raise FoundryError(f"version {version} failed: {json.dumps(get_version(name, version))[:3000]}")
        time.sleep(15)
    raise FoundryError(f"version {version} not active after {timeout_s}s (last status {status})")


def pin(name: str, version: str, expect_current: str | None) -> dict:
    current = pinned_version(name)
    if expect_current is not None and current != expect_current:
        raise FoundryError(
            f"compare-and-swap failed: production is pinned to {current!r}, expected {expect_current!r}. "
            "Someone else promoted in the meantime; re-run the release."
        )
    body = {
        "agent_endpoint": {
            "version_selector": {
                "version_selection_rules": [
                    {"type": "FixedRatio", "agent_version": version, "traffic_percentage": 100}
                ]
            },
            "protocol_configuration": {"responses": {}},
        }
    }
    _request("PATCH", f"agents/{name}", headers={"Content-Type": "application/merge-patch+json"}, data=json.dumps(body))
    after = pinned_version(name)
    if after != version:
        raise FoundryError(f"pin did not take effect: pinned={after!r}, wanted {version!r}")
    return {"previous": current, "pinned": after}


def invoke(name: str, version: str | None, text: str, *, user: str | None = None) -> dict:
    body: dict = {"input": text, "stream": False}
    if version:
        body["agent_reference"] = {"type": "agent_reference", "name": name, "version": version}
    if user:
        body["user"] = user
    resp = _request("POST", f"agents/{name}/endpoint/protocols/openai/responses", json=body, retries=2)
    payload = resp.json()
    payload["_served_version"] = resp.headers.get("x-ms-agent-version") or (
        (payload.get("agent_reference") or {}).get("version")
    )
    return payload


def output_text(response: dict) -> str:
    if response.get("output_text"):
        return response["output_text"]
    parts: list[str] = []
    for item in response.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    parts.append(c["text"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def emit(result: dict, github_output: bool) -> None:
    print(json.dumps(result, indent=2))
    if github_output and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            for k, v in result.items():
                if isinstance(v, (str, int, float)) or v is None:
                    fh.write(f"{k}={'' if v is None else v}\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agent", default=os.environ.get("AGENT_NAME", "clinical-agent"))
    p.add_argument("--github-output", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("identity")
    sub.add_parser("selector")
    sub.add_parser("versions")
    cv = sub.add_parser("create-version")
    cv.add_argument("--code-dir", default=str(ROOT / "agent"))
    cv.add_argument("--template-version")
    cv.add_argument("--description", default=os.environ.get("GITHUB_SHA", "local"))
    cv.add_argument("--no-wait", action="store_true")
    pn = sub.add_parser("pin")
    pn.add_argument("--version", required=True)
    pn.add_argument("--expect-current")
    iv = sub.add_parser("invoke")
    iv.add_argument("--version")
    iv.add_argument("text")
    a = p.parse_args(argv)

    try:
        if a.cmd == "identity":
            emit({"principal_id": identity_principal(a.agent)}, a.github_output)
        elif a.cmd == "selector":
            emit({"pinned_version": pinned_version(a.agent), "latest_version": latest_version(a.agent)}, a.github_output)
        elif a.cmd == "versions":
            rows = [
                {"version": v.get("version"), "status": v.get("status"), "description": v.get("description"),
                 "created_at": v.get("created_at")}
                for v in list_versions(a.agent)
            ]
            print(json.dumps(rows, indent=2))
        elif a.cmd == "create-version":
            env = {"AGENT_GIT_SHA": os.environ.get("GITHUB_SHA", "local")[:12]}
            result = create_version(
                a.agent, Path(a.code_dir), template_version=a.template_version, description=a.description,
                env_overrides=env,
            )
            if not a.no_wait:
                result["status"] = wait_active(a.agent, result["version"])
            emit(result, a.github_output)
        elif a.cmd == "pin":
            emit(pin(a.agent, a.version, a.expect_current), a.github_output)
        elif a.cmd == "invoke":
            resp = invoke(a.agent, a.version, a.text)
            emit({"served_version": resp.get("_served_version"), "id": resp.get("id"),
                  "text": output_text(resp)}, a.github_output)
    except FoundryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
