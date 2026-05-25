"""Bounded LLM routing-pattern planner over compact discovery artifacts."""

from __future__ import annotations

import json
from typing import Any

from sydes.llm.client import LLMClient, LLMClientError, LLMRequest

_MAX_PROMPT_CHARS = 12_000


def _trim_snippet(value: str, *, max_chars: int = 300) -> str:
    text = " ".join(value.strip().split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def select_representative_snippets(
    route_index_repo: dict,
    *,
    max_router_declarations: int = 5,
    max_route_declarations: int = 12,
    max_mount_calls: int = 5,
) -> dict:
    """Select bounded, diverse snippets from route-index facts."""
    files = route_index_repo.get("files", []) if isinstance(route_index_repo, dict) else []

    router_declarations: list[dict[str, Any]] = []
    route_declarations: list[dict[str, Any]] = []
    mount_calls: list[dict[str, Any]] = []
    seen_router_files: set[str] = set()
    seen_route_receivers: set[str] = set()
    seen_mount_files: set[str] = set()

    for file_item in files:
        if not isinstance(file_item, dict):
            continue
        path = file_item.get("path")
        if not isinstance(path, str):
            continue

        symbols = file_item.get("router_symbols") or []
        if isinstance(symbols, list):
            for symbol in symbols:
                if not isinstance(symbol, str):
                    continue
                if len(router_declarations) >= max_router_declarations:
                    break
                if path in seen_router_files and symbol in {item.get("symbol") for item in router_declarations}:
                    continue
                router_declarations.append(
                    {
                        "file": path,
                        "symbol": symbol,
                        "snippet": f"router symbol {symbol}",
                    }
                )
                seen_router_files.add(path)

        route_calls = file_item.get("route_calls") or []
        if isinstance(route_calls, list):
            for call in route_calls:
                if len(route_declarations) >= max_route_declarations:
                    break
                if not isinstance(call, dict):
                    continue
                receiver = call.get("receiver")
                snippet = call.get("snippet")
                if not isinstance(receiver, str) or not isinstance(snippet, str):
                    continue
                if receiver in seen_route_receivers and path in {item.get("file") for item in route_declarations}:
                    continue
                route_declarations.append(
                    {
                        "file": path,
                        "receiver": receiver,
                        "method": call.get("method"),
                        "path": call.get("path"),
                        "line": call.get("line"),
                        "snippet": _trim_snippet(snippet),
                    }
                )
                seen_route_receivers.add(receiver)

        mounts = file_item.get("mount_calls") or []
        if isinstance(mounts, list):
            for mount in mounts:
                if len(mount_calls) >= max_mount_calls:
                    break
                if not isinstance(mount, dict):
                    continue
                snippet = mount.get("snippet")
                if not isinstance(snippet, str):
                    continue
                if path in seen_mount_files:
                    continue
                mount_calls.append(
                    {
                        "file": path,
                        "receiver": mount.get("receiver"),
                        "prefix": mount.get("prefix"),
                        "child": mount.get("child"),
                        "line": mount.get("line"),
                        "snippet": _trim_snippet(snippet),
                    }
                )
                seen_mount_files.add(path)

    return {
        "router_declarations": router_declarations[:max_router_declarations],
        "route_declarations": route_declarations[:max_route_declarations],
        "mount_calls": mount_calls[:max_mount_calls],
    }


def build_routing_pattern_planner_input(
    *,
    repo_name: str,
    repo_map_repo: dict | None,
    route_index_repo: dict | None,
    route_graph_repo: dict | None,
    coverage: dict,
) -> dict:
    """Build bounded planner input from compact discovery artifacts."""
    route_index_repo = route_index_repo or {}
    route_graph_repo = route_graph_repo or {}
    repo_map_repo = repo_map_repo or {}

    snippets = select_representative_snippets(route_index_repo)

    payload = {
        "repo": repo_name,
        "repo_map": {
            "candidate_backend_dirs": repo_map_repo.get("candidate_backend_dirs", []),
            "candidate_route_dirs": repo_map_repo.get("candidate_route_dirs", []),
            "candidate_controller_dirs": repo_map_repo.get("candidate_controller_dirs", []),
            "entrypoint_candidates": repo_map_repo.get("entrypoint_candidates", []),
            "manifests": repo_map_repo.get("manifests", []),
            "ignored_dirs": repo_map_repo.get("ignored_dirs", []),
            "summary": repo_map_repo.get("summary", {}),
        },
        "route_index_summary": route_index_repo.get("summary", {}),
        "route_graph_summary": route_graph_repo.get("summary", {}),
        "coverage": coverage,
        "snippets": snippets,
    }

    raw = json.dumps(payload, separators=(",", ":"))
    if len(raw) > _MAX_PROMPT_CHARS:
        # Tighten snippet lists first; preserve summaries.
        snippets["route_declarations"] = snippets["route_declarations"][:6]
        snippets["mount_calls"] = snippets["mount_calls"][:3]
        snippets["router_declarations"] = snippets["router_declarations"][:3]
        payload["snippets"] = snippets
    return payload


def build_routing_pattern_planner_prompt(planner_input: dict) -> str:
    """Build strict JSON-only prompt for routing pattern planning."""
    return (
        "Task: infer routing conventions from compact repository discovery facts.\n"
        "Do NOT enumerate all routes.\n"
        "Do NOT request more files.\n"
        "Focus on framework family, declaration syntax, mount strategy, path param style, and next extraction action.\n"
        "Return JSON only with this shape:\n"
        '{"version":"v1","repo":"","framework_family":"","routing_convention":"","confidence":0.0,'
        '"route_container_patterns":[{"kind":"","pattern":"","evidence_files":[]}],'
        '"route_declaration_patterns":[{"kind":"","pattern":"","methods":[],"path_param_style":"","handler_hint":""}],'
        '"mount_patterns":[{"kind":"","pattern":"","prefix_arg":"","child_router_arg":""}],'
        '"entrypoint_hints":[],"route_dir_hints":[],"ignore_hints":[],"risks":[],"recommended_next_action":""}\n'
        "Keep confidence realistic (0..1).\n"
        "Input:\n"
        f"{json.dumps(planner_input, separators=(',', ':'))}\n"
    )


def _extract_json_payload(text: str) -> Any | None:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            stripped = "\n".join(lines[1:-1]).strip()
    if stripped.startswith("```json"):
        stripped = stripped[len("```json") :].strip()
    if stripped.startswith("```"):
        stripped = stripped[3:].strip()
    if stripped.endswith("```"):
        stripped = stripped[:-3].strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def validate_routing_pattern_plan(payload: Any) -> dict:
    """Validate planner output shape and raise ValueError on malformed payload."""
    if not isinstance(payload, dict):
        raise ValueError("Planner output must be a JSON object.")

    required_str = [
        "version",
        "repo",
        "framework_family",
        "routing_convention",
        "recommended_next_action",
    ]
    for key in required_str:
        if key not in payload:
            raise ValueError(f"Planner output missing required field: {key}")
        if not isinstance(payload[key], str):
            raise ValueError(f"Planner field '{key}' must be a string.")

    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)):
        raise ValueError("Planner field 'confidence' must be numeric.")

    required_lists = [
        "route_container_patterns",
        "route_declaration_patterns",
        "mount_patterns",
        "entrypoint_hints",
        "route_dir_hints",
        "ignore_hints",
        "risks",
    ]
    for key in required_lists:
        if key not in payload or not isinstance(payload[key], list):
            raise ValueError(f"Planner field '{key}' must be a list.")

    return payload


def run_routing_pattern_planner(
    *,
    repo_name: str,
    planner_input: dict,
    llm_client: LLMClient,
) -> dict:
    """Run bounded planner and return validated plan payload."""
    prompt = build_routing_pattern_planner_prompt(planner_input)
    response = llm_client.generate(LLMRequest(prompt=prompt, temperature=0.0))
    payload = _extract_json_payload(response.text)
    if payload is None:
        raise LLMClientError("Routing pattern planner output was not valid JSON.")
    plan = validate_routing_pattern_plan(payload)
    plan.setdefault("version", "v1")
    plan["repo"] = repo_name
    return plan
