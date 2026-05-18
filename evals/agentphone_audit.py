"""Controlled AgentPhone API audit and fuzz harness.

This intentionally avoids high-volume "hammering" defaults. It discovers the
current AgentPhone API from the published OpenAPI document, builds probes for
every operation, and runs them with a hard request budget and rate limit.

Examples:
    uv run python -m evals.agentphone_audit --dry-run
    uv run python -m evals.agentphone_audit --live --profile standard --max-requests 120
    uv run python -m evals.agentphone_audit --live --rpm 1000 --max-requests 500
    uv run python -m evals.agentphone_audit --live --include-mutating --max-requests 200

Mutating live probes use malformed JSON or invalid path ids by default so the
server exercises validation and auth paths without creating billable resources.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_OPENAPI_URL: Final[str] = "https://docs.agentphone.ai/openapi.json"
DEFAULT_API_BASE_URL: Final[str] = "https://api.agentphone.ai"
DEFAULT_OUT_DIR: Final[Path] = ROOT / "evals" / "reports"

HTTP_METHODS: Final[frozenset[str]] = frozenset({"get", "post", "patch", "put", "delete"})
SENSITIVE_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"(authorization|api[_-]?key|secret|token|password|signature|phone|number)",
    re.IGNORECASE,
)
PATH_PARAM_RE: Final[re.Pattern[str]] = re.compile(r"{([^}]+)}")

Risk = Literal["read", "mutating", "billable", "destructive", "external"]
FindingSeverity = Literal["critical", "high", "medium", "low", "info"]


@dataclass(frozen=True)
class Operation:
    """One OpenAPI operation."""

    method: str
    path: str
    operation_id: str
    summary: str
    parameters: tuple[dict[str, Any], ...]
    has_body: bool
    risks: tuple[Risk, ...]


@dataclass(frozen=True)
class Probe:
    """One generated HTTP request."""

    operation: Operation
    name: str
    path_values: Mapping[str, str] = field(default_factory=dict)
    query: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    auth: Literal["valid", "missing", "bogus"] = "valid"
    expected: str = "No 5xx; auth failures should be 401/403; validation failures 400/422."
    allow_live: bool = True


@dataclass(frozen=True)
class Result:
    """One completed or skipped probe."""

    timestamp: str
    method: str
    path: str
    operation_id: str
    probe: str
    url: str
    status_code: int | None
    elapsed_ms: float | None
    outcome: str
    risks: tuple[Risk, ...]
    response_bytes: int | None = None
    content_type: str | None = None
    body_sample: Any | None = None
    error: str | None = None


@dataclass(frozen=True)
class Finding:
    """A possible security or robustness issue found by a probe."""

    severity: FindingSeverity
    title: str
    operation_id: str
    method: str
    path: str
    probe: str
    detail: str


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _load_openapi(url: str, timeout: float) -> dict[str, Any]:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        decoded = response.json()
    if not isinstance(decoded, dict):
        msg = "OpenAPI response was not a JSON object"
        raise TypeError(msg)
    return cast("dict[str, Any]", decoded)


def _operation_risks(method: str, path: str) -> tuple[Risk, ...]:
    risks: list[Risk] = []
    if method == "GET":
        risks.append("read")
    else:
        risks.append("mutating")

    if method == "DELETE" or path.endswith("/end"):
        risks.append("destructive")

    billable_posts = {"/v1/numbers", "/v1/calls", "/v1/calls/web", "/v1/messages"}
    if (method == "POST" and path in billable_posts) or path.endswith(("/reactions", "/typing")):
        risks.append("billable")

    if (
        "/webhook" in path
        or path in {"/v0/agent/sign-up", "/v0/agent/verify"}
        or path.endswith(
            ("/messages", "/reactions", "/typing"),
        )
        or "/calls" in path
    ):
        risks.append("external")

    return tuple(dict.fromkeys(risks))


def _iter_operations(openapi: Mapping[str, Any]) -> list[Operation]:
    paths = openapi.get("paths")
    if not isinstance(paths, dict):
        msg = "OpenAPI document does not contain a paths object"
        raise TypeError(msg)

    operations: list[Operation] = []
    for path, path_item in sorted(paths.items()):
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        for method, spec in sorted(path_item.items()):
            method_l = method.lower()
            if method_l not in HTTP_METHODS or not isinstance(spec, dict):
                continue
            parameters = spec.get("parameters", [])
            if not isinstance(parameters, list):
                parameters = []
            operation_id = str(spec.get("operationId") or f"{method_l}-{path}")
            summary = str(spec.get("summary") or "")
            operations.append(
                Operation(
                    method=method_l.upper(),
                    path=path,
                    operation_id=operation_id,
                    summary=summary,
                    parameters=tuple(cast("list[dict[str, Any]]", parameters)),
                    has_body=isinstance(spec.get("requestBody"), dict),
                    risks=_operation_risks(method_l.upper(), path),
                ),
            )
    return operations


def _path_params(path: str) -> tuple[str, ...]:
    return tuple(PATH_PARAM_RE.findall(path))


def _query_params(operation: Operation) -> list[dict[str, Any]]:
    return [param for param in operation.parameters if param.get("in") == "query"]


def _default_query(operation: Operation) -> dict[str, str]:
    query: dict[str, str] = {}
    for param in _query_params(operation):
        name = param.get("name")
        if not isinstance(name, str):
            continue
        schema = param.get("schema")
        schema_type = schema.get("type") if isinstance(schema, dict) else None
        if name == "limit" or schema_type == "integer":
            query[name] = "1"
        elif schema_type == "boolean":
            query[name] = "false"
        else:
            query[name] = "audit"
    return query


def _resolved_path(path: str, values: Mapping[str, str]) -> str:
    resolved = path
    for name in _path_params(path):
        value = values.get(name, f"missing-{name}")
        resolved = resolved.replace("{" + name + "}", quote(value, safe="%"))
    return resolved


def _resource_values(
    path: str,
    known: Mapping[str, str],
    *,
    fallback_prefix: str,
) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in _path_params(path):
        values[name] = known.get(name) or f"{fallback_prefix}_{uuid.uuid4().hex[:20]}"
    return values


def _safe_invalid_values(profile: str) -> list[str]:
    values = [
        f"missing_{uuid.uuid4().hex[:20]}",
        "0",
        "not-a-real-id",
    ]
    if profile in {"standard", "aggressive"}:
        values.extend(
            [
                "..%2F..%2Fetc%2Fpasswd",
                quote("null\x00byte", safe=""),
                "x" * 256,
            ],
        )
    if profile == "aggressive":
        values.extend(
            [
                "%252e%252e%252f",
                "admin",
                "true",
                str(2**63 - 1),
                "\u2603",
            ],
        )
    return values


def _query_fuzz_values(schema_type: str | None, profile: str) -> list[str]:
    if schema_type == "integer":
        values = ["-1", "0", "999999999", "not-int"]
    elif schema_type == "boolean":
        values = ["true", "false", "1", "not-bool"]
    else:
        values = ["", "x" * 256, "%27%22%3C%3E"]
    if profile == "aggressive":
        values.extend(["null", "[]", "{}"])
    return values


def _auth_probes(operation: Operation) -> list[Probe]:
    values = _resource_values(operation.path, {}, fallback_prefix="auth_missing")
    body = b'{"audit":"auth"}' if operation.has_body else None
    headers = {"Content-Type": "application/json"} if body else {}
    return [
        Probe(
            operation=operation,
            name="missing-auth",
            path_values=values,
            query=_default_query(operation),
            headers=headers,
            body=body,
            auth="missing",
            expected="Protected endpoints should reject missing Authorization.",
        ),
        Probe(
            operation=operation,
            name="bogus-auth",
            path_values=values,
            query=_default_query(operation),
            headers=headers,
            body=body,
            auth="bogus",
            expected="Protected endpoints should reject an invalid bearer token.",
        ),
    ]


def _read_probes(operation: Operation, known: Mapping[str, str], profile: str) -> list[Probe]:
    probes: list[Probe] = []
    baseline_values = _resource_values(operation.path, known, fallback_prefix="missing")
    probes.append(
        Probe(
            operation=operation,
            name="read-baseline",
            path_values=baseline_values,
            query=_default_query(operation),
        ),
    )

    params = _query_params(operation)
    for param in params:
        name = param.get("name")
        if not isinstance(name, str):
            continue
        schema = param.get("schema")
        schema_type = schema.get("type") if isinstance(schema, dict) else None
        for value in _query_fuzz_values(cast("str | None", schema_type), profile):
            query = _default_query(operation)
            query[name] = value
            probes.append(
                Probe(
                    operation=operation,
                    name=f"query-fuzz:{name}={value[:24]}",
                    path_values=baseline_values,
                    query=query,
                    expected="Bad query values should produce 400/422, not 5xx.",
                ),
            )

    path_param_names = _path_params(operation.path)
    if path_param_names:
        probes.extend(
            Probe(
                operation=operation,
                name=f"path-id-fuzz:{value[:24]}",
                path_values=dict.fromkeys(path_param_names, value),
                query=_default_query(operation),
                expected="Bad path ids should produce 400/404/422, not 5xx.",
            )
            for value in _safe_invalid_values(profile)
        )
    return probes


def _mutating_validation_probes(
    operation: Operation,
    known: Mapping[str, str],
    *,
    include_destructive: bool,
) -> list[Probe]:
    if "destructive" in operation.risks and not include_destructive:
        return [
            Probe(
                operation=operation,
                name="skipped-destructive",
                allow_live=False,
                expected="Skipped unless --include-destructive is supplied.",
            ),
        ]

    values = _resource_values(operation.path, known, fallback_prefix="missing")
    if operation.method == "DELETE":
        if not _path_params(operation.path):
            return [
                Probe(
                    operation=operation,
                    name="skipped-collection-delete",
                    allow_live=False,
                    expected="Collection DELETE is skipped to avoid removing configured webhooks.",
                ),
            ]
        values = _resource_values(operation.path, {}, fallback_prefix="delete_missing")
        return [
            Probe(
                operation=operation,
                name="invalid-id-delete",
                path_values=values,
                expected="Deleting a nonexistent id should be 404/422, not 2xx or 5xx.",
            ),
        ]

    return [
        Probe(
            operation=operation,
            name="malformed-json",
            path_values=values,
            headers={"Content-Type": "application/json"},
            body=b'{"audit":',
            expected="Malformed JSON should be rejected before side effects.",
        ),
        Probe(
            operation=operation,
            name="wrong-json-type",
            path_values=values,
            headers={"Content-Type": "application/json"},
            body=b'"not-an-object"',
            expected="Wrong JSON type should be 400/422, not 2xx or 5xx.",
        ),
    ]


def _generate_probes(
    operations: Sequence[Operation],
    *,
    known: Mapping[str, str],
    profile: str,
    include_mutating: bool,
    include_destructive: bool,
    include_auth_matrix: bool,
) -> list[Probe]:
    probes: list[Probe] = []
    for operation in operations:
        if include_auth_matrix and not operation.path.startswith("/v0/"):
            probes.extend(_auth_probes(operation))

        if operation.method == "GET":
            probes.extend(_read_probes(operation, known, profile))
            continue

        if include_mutating:
            probes.extend(
                _mutating_validation_probes(
                    operation,
                    known,
                    include_destructive=include_destructive,
                ),
            )
    return probes


def _sanitize(value: object, *, max_string: int = 500) -> object:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            if SENSITIVE_KEY_RE.search(key_s):
                out[key_s] = "<redacted>"
            else:
                out[key_s] = _sanitize(item, max_string=max_string)
        return out
    if isinstance(value, list):
        return [_sanitize(item, max_string=max_string) for item in value[:20]]
    if isinstance(value, str):
        if len(value) > max_string:
            return value[:max_string] + "...<truncated>"
        return value
    return value


def _response_sample(response: httpx.Response) -> object | None:
    content = response.content
    if not content:
        return None
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            return _sanitize(response.json())
        except json.JSONDecodeError:
            pass
    text = content[:1000].decode(errors="replace")
    return _sanitize(text)


def _base_url_join(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1") and path.startswith("/v1/"):
        path = path.removeprefix("/v1")
    if base.endswith("/v0") and path.startswith("/v0/"):
        path = path.removeprefix("/v0")
    return f"{base}{path}"


def _probe_url(base_url: str, probe: Probe) -> str:
    return _base_url_join(base_url, _resolved_path(probe.operation.path, probe.path_values))


def _headers_for_probe(probe: Probe, api_key: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "jailcall-agentphone-audit/0.1",
    }
    headers.update(probe.headers)
    if probe.auth == "valid":
        headers["Authorization"] = f"Bearer {api_key}"
    elif probe.auth == "bogus":
        headers["Authorization"] = "Bearer invalid-audit-token"
    return headers


def _run_probe(client: httpx.Client, probe: Probe, *, api_key: str, base_url: str) -> Result:
    url = _probe_url(base_url, probe)
    started = time.perf_counter()
    try:
        response = client.request(
            probe.operation.method,
            url,
            params=probe.query,
            content=probe.body,
            headers=_headers_for_probe(probe, api_key),
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        return Result(
            timestamp=_utc_now(),
            method=probe.operation.method,
            path=probe.operation.path,
            operation_id=probe.operation.operation_id,
            probe=probe.name,
            url=url,
            status_code=response.status_code,
            elapsed_ms=round(elapsed_ms, 2),
            outcome="completed",
            risks=probe.operation.risks,
            response_bytes=len(response.content),
            content_type=response.headers.get("content-type"),
            body_sample=_response_sample(response),
        )
    except httpx.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return Result(
            timestamp=_utc_now(),
            method=probe.operation.method,
            path=probe.operation.path,
            operation_id=probe.operation.operation_id,
            probe=probe.name,
            url=url,
            status_code=None,
            elapsed_ms=round(elapsed_ms, 2),
            outcome="transport-error",
            risks=probe.operation.risks,
            error=type(exc).__name__ + ": " + str(exc),
        )


def _skip_result(probe: Probe, *, base_url: str, reason: str) -> Result:
    return Result(
        timestamp=_utc_now(),
        method=probe.operation.method,
        path=probe.operation.path,
        operation_id=probe.operation.operation_id,
        probe=probe.name,
        url=_probe_url(base_url, probe),
        status_code=None,
        elapsed_ms=None,
        outcome="skipped",
        risks=probe.operation.risks,
        error=reason,
    )


def _classify(result: Result) -> list[Finding]:
    if result.outcome == "skipped":
        return []
    if result.status_code is None:
        return [
            Finding(
                severity="medium",
                title="Transport error",
                operation_id=result.operation_id,
                method=result.method,
                path=result.path,
                probe=result.probe,
                detail=result.error or "Request failed before a response was received.",
            ),
        ]

    findings: list[Finding] = []
    if result.status_code >= 500:
        findings.append(
            Finding(
                severity="high",
                title="Server error from fuzz probe",
                operation_id=result.operation_id,
                method=result.method,
                path=result.path,
                probe=result.probe,
                detail=f"Received HTTP {result.status_code}.",
            ),
        )

    if result.probe in {"missing-auth", "bogus-auth"} and result.status_code < 400:
        findings.append(
            Finding(
                severity="critical",
                title="Protected endpoint accepted missing or bogus auth",
                operation_id=result.operation_id,
                method=result.method,
                path=result.path,
                probe=result.probe,
                detail=f"Auth probe received HTTP {result.status_code}.",
            ),
        )

    if result.probe in {"malformed-json", "wrong-json-type"} and result.status_code < 400:
        findings.append(
            Finding(
                severity="high",
                title="Mutating endpoint accepted invalid request body",
                operation_id=result.operation_id,
                method=result.method,
                path=result.path,
                probe=result.probe,
                detail=f"Invalid body probe received HTTP {result.status_code}.",
            ),
        )

    if result.probe == "invalid-id-delete" and result.status_code < 400:
        findings.append(
            Finding(
                severity="high",
                title="Delete endpoint returned success for invalid id",
                operation_id=result.operation_id,
                method=result.method,
                path=result.path,
                probe=result.probe,
                detail=f"Invalid id delete received HTTP {result.status_code}.",
            ),
        )

    if result.elapsed_ms is not None and result.elapsed_ms > 10_000:
        findings.append(
            Finding(
                severity="low",
                title="Slow response",
                operation_id=result.operation_id,
                method=result.method,
                path=result.path,
                probe=result.probe,
                detail=f"Response took {result.elapsed_ms} ms.",
            ),
        )

    return findings


def _write_jsonl(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            serialized: object = row
            if hasattr(row, "__dataclass_fields__"):
                serialized = asdict(row)
            fh.write(json.dumps(_sanitize(serialized), sort_keys=True) + "\n")


def _write_summary(path: Path, *, results: Sequence[Result], findings: Sequence[Finding]) -> None:
    status_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}

    for result in results:
        status = str(result.status_code) if result.status_code is not None else "none"
        status_counts[status] = status_counts.get(status, 0) + 1
        outcome_counts[result.outcome] = outcome_counts.get(result.outcome, 0) + 1
    for finding in findings:
        severity_counts[finding.severity] = severity_counts.get(finding.severity, 0) + 1

    summary = {
        "generated_at": _utc_now(),
        "results": len(results),
        "findings": len(findings),
        "status_counts": status_counts,
        "outcome_counts": outcome_counts,
        "severity_counts": severity_counts,
        "top_findings": [asdict(finding) for finding in findings[:25]],
    }
    path.write_text(
        json.dumps(_sanitize(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _known_resources_from_args(values: Sequence[str]) -> dict[str, str]:
    known: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            msg = f"--resource must be name=value, got {raw!r}"
            raise ValueError(msg)
        name, value = raw.split("=", 1)
        if not name or not value:
            msg = f"--resource must be name=value, got {raw!r}"
            raise ValueError(msg)
        known[name] = value
    env_map = {
        "agent_id": "AGENTPHONE_AGENT_ID",
        "number_id": "AGENTPHONE_NUMBER_ID",
    }
    for param_name, env_name in env_map.items():
        value = os.environ.get(env_name)
        if value and param_name not in known:
            known[param_name] = value
    return known


def _select_profile_limit(profile: str) -> int:
    if profile == "smoke":
        return 1
    if profile == "standard":
        return 4
    return 10


def _cap_probes_per_operation(probes: Sequence[Probe], *, profile: str) -> list[Probe]:
    per_kind_limit = _select_profile_limit(profile)
    counts: dict[tuple[str, str], int] = {}
    capped: list[Probe] = []
    for probe in probes:
        if probe.name in {"missing-auth", "bogus-auth", "read-baseline"}:
            capped.append(probe)
            continue
        kind = probe.name.split(":", 1)[0]
        key = (probe.operation.operation_id, kind)
        count = counts.get(key, 0)
        if count >= per_kind_limit:
            continue
        counts[key] = count + 1
        capped.append(probe)
    return capped


def _is_allowed_host(base_url: str) -> bool:
    return base_url.rstrip("/").startswith("https://api.agentphone.ai")


def _print_plan(operations: Sequence[Operation], probes: Sequence[Probe]) -> None:
    risk_counts: dict[str, int] = {}
    method_counts: dict[str, int] = {}
    for operation in operations:
        method_counts[operation.method] = method_counts.get(operation.method, 0) + 1
        for risk in operation.risks:
            risk_counts[risk] = risk_counts.get(risk, 0) + 1

    live_count = sum(1 for probe in probes if probe.allow_live)
    skipped_count = len(probes) - live_count
    print(f"Discovered {len(operations)} operations.")
    print(
        "Methods: "
        + ", ".join(f"{method}={count}" for method, count in sorted(method_counts.items())),
    )
    print("Risks: " + ", ".join(f"{risk}={count}" for risk, count in sorted(risk_counts.items())))
    print(
        f"Generated {len(probes)} probes "
        f"({live_count} runnable, {skipped_count} explicitly skipped).",
    )


def _run(
    *,
    args: argparse.Namespace,
    probes: Sequence[Probe],
    api_key: str,
) -> tuple[list[Result], list[Finding]]:
    selected = list(probes)
    if args.shuffle:
        random.shuffle(selected)
    selected = selected[: args.max_requests]

    results: list[Result] = []
    findings: list[Finding] = []
    min_interval = 60.0 / args.rpm
    last_request_at = 0.0

    out_prefix = args.out
    result_path = out_prefix.with_suffix(".results.jsonl")
    finding_path = out_prefix.with_suffix(".findings.jsonl")
    summary_path = out_prefix.with_suffix(".summary.json")

    if args.dry_run:
        for probe in selected:
            reason = "dry-run"
            if not probe.allow_live:
                reason = probe.expected
            results.append(_skip_result(probe, base_url=args.base_url, reason=reason))
    else:
        with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
            for idx, probe in enumerate(selected, start=1):
                if not probe.allow_live:
                    result = _skip_result(probe, base_url=args.base_url, reason=probe.expected)
                else:
                    elapsed_since_last = time.perf_counter() - last_request_at
                    if elapsed_since_last < min_interval:
                        time.sleep(min_interval - elapsed_since_last)
                    result = _run_probe(client, probe, api_key=api_key, base_url=args.base_url)
                    last_request_at = time.perf_counter()
                results.append(result)
                findings.extend(_classify(result))
                if idx % args.progress_every == 0 or idx == len(selected):
                    print(f"{idx}/{len(selected)} probes complete")

    _write_jsonl(result_path, results)
    _write_jsonl(finding_path, findings)
    _write_summary(summary_path, results=results, findings=findings)

    print(f"Results:  {result_path}")
    print(f"Findings: {finding_path}")
    print(f"Summary:  {summary_path}")
    return results, findings


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print and save the probe plan only.")
    mode.add_argument("--live", action="store_true", help="Send bounded live requests.")
    parser.add_argument("--openapi-url", default=DEFAULT_OPENAPI_URL)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AGENTPHONE_API_BASE_URL", DEFAULT_API_BASE_URL),
    )
    parser.add_argument("--api-key-env", default="AGENTPHONE_API_KEY")
    parser.add_argument("--profile", choices=["smoke", "standard", "aggressive"], default="smoke")
    parser.add_argument("--max-requests", type=int, default=120)
    parser.add_argument(
        "--rpm",
        type=float,
        default=60.0,
        help="Requests per minute. Hard-capped at 1000.",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument(
        "--include-mutating",
        action="store_true",
        help="Include validation-only probes for POST/PATCH/PUT/DELETE endpoints.",
    )
    parser.add_argument(
        "--include-destructive",
        action="store_true",
        help="Allow invalid-id DELETE/end-call probes. Real collection deletes are still skipped.",
    )
    parser.add_argument(
        "--no-auth-matrix",
        action="store_true",
        help="Skip missing/bogus Authorization checks.",
    )
    parser.add_argument(
        "--resource",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Known path id, e.g. agent_id=... number_id=... call_id=...",
    )
    parser.add_argument("--shuffle", action="store_true")
    parser.add_argument("--allow-any-host", action="store_true")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR / f"agentphone_audit_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}",
    )
    args = parser.parse_args(argv)

    if not args.dry_run and not args.live:
        args.dry_run = True
    if args.max_requests < 1:
        parser.error("--max-requests must be >= 1")
    if args.rpm <= 0:
        parser.error("--rpm must be > 0")
    if args.rpm > 1000:
        parser.error("--rpm is hard-capped at 1000 for this harness")
    if args.progress_every < 1:
        parser.error("--progress-every must be >= 1")
    if not args.allow_any_host and not _is_allowed_host(args.base_url):
        parser.error("--base-url must be https://api.agentphone.ai unless --allow-any-host is set")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Run the AgentPhone audit CLI."""
    load_dotenv(ROOT / ".env")
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    api_key = os.environ.get(args.api_key_env, "")
    if args.live and not api_key:
        print(f"Missing {args.api_key_env}; populate .env or run --dry-run.", file=sys.stderr)
        return 2

    try:
        known = _known_resources_from_args(args.resource)
        openapi = _load_openapi(args.openapi_url, args.timeout)
        operations = _iter_operations(openapi)
        probes = _generate_probes(
            operations,
            known=known,
            profile=args.profile,
            include_mutating=args.include_mutating,
            include_destructive=args.include_destructive,
            include_auth_matrix=not args.no_auth_matrix,
        )
        probes = _cap_probes_per_operation(probes, profile=args.profile)
        _print_plan(operations, probes)
        _results, findings = _run(args=args, probes=probes, api_key=api_key)
    except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
        print(f"agentphone audit failed: {exc}", file=sys.stderr)
        return 1

    critical_or_high = [finding for finding in findings if finding.severity in {"critical", "high"}]
    if critical_or_high:
        print(f"{len(critical_or_high)} critical/high findings.")
        return 3
    print(f"{len(findings)} findings total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
