"""Routes CLI plumbing for V1 placeholder endpoint discovery."""

from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Annotated, Literal

import typer

from sydes.cli.output_paths import resolve_output_file_path, write_output_text
from sydes.core.models import RoutesResult
from sydes.discover.endpoints import discover_endpoints
from sydes.discover.discovery_coverage import evaluate_discovery_coverage
from sydes.discover.discovery_cache import (
    ARTIFACT_NAMES as CACHEABLE_ARTIFACT_NAMES,
    load_cache_bundle,
    save_cache_bundle,
)
from sydes.discover.route_graph import build_route_graph_facts_batch
from sydes.discover.route_index import build_route_index_batch
from sydes.discover.repo_map import build_repo_map_batch
from sydes.discover.routing_pattern_planner import (
    build_routing_pattern_planner_input,
    run_routing_pattern_planner,
)
from sydes.discover.routing_pattern_executor import execute_routing_pattern_plan
from sydes.ingest.repos import parse_repo_specs
from sydes.llm.client import (
    LLMClient,
    LLMClientError,
    create_default_llm_client,
    validate_llm_available,
)
from sydes.report.json_report import render_routes_json
from sydes.report.terminal import render_routes_terminal
from sydes.store.workspace import compute_workspace_id, create_run_id, save_run_artifact


def _write_command_output(
    output: Path,
    content: str,
    *,
    output_format: Literal["terminal", "json"],
) -> None:
    """Resolve and write command output without leaking low-level path errors."""
    default_name = "routes.json" if output_format == "json" else "routes.txt"
    resolved = resolve_output_file_path(output, default_filename=default_name)
    write_output_text(resolved, content)


def _extract_repo_note_int(notes: list[str], repo_name: str, field: str) -> int:
    pattern = re.compile(rf"^{re.escape(repo_name)}:\s.*\b{re.escape(field)}=(\d+)")
    for note in notes:
        match = pattern.search(note)
        if match:
            return int(match.group(1))
    return 0


def _repo_index_by_name(payload: dict | None, key: str) -> dict[str, dict]:
    if not isinstance(payload, dict):
        return {}
    items = payload.get(key, [])
    if not isinstance(items, list):
        return {}
    return {
        item.get("repo"): item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("repo"), str)
    }


def _normalize_identity_path(path: str | None) -> str:
    if path is None:
        return ""
    value = path.strip()
    if not value:
        return ""
    if not value.startswith("/"):
        value = "/" + value
    value = re.sub(r"/+", "/", value)
    if value != "/" and value.endswith("/"):
        value = value[:-1]
    value = re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", value)
    value = re.sub(r":([A-Za-z_]\w*)", r"{\1}", value)
    return value


def _merge_route_lists(existing: list, extra: list) -> list:
    merged: dict[tuple[str, str, str], object] = {}
    for route in [*existing, *extra]:
        method = (getattr(route, "method", None) or "").upper()
        path = _normalize_identity_path(getattr(route, "path", None))
        repo = getattr(route, "repo", "")
        key = (repo, method, path)
        if key not in merged:
            merged[key] = route
            continue
        current = merged[key]
        current_status = getattr(current, "status", "") or ""
        new_status = getattr(route, "status", "") or ""
        if current_status.startswith("deterministic_plan"):
            continue
        if new_status.startswith("deterministic_plan"):
            merged[key] = route
            continue
        if getattr(current, "handler", None) is None and getattr(route, "handler", None) is not None:
            merged[key] = route
    return sorted(
        merged.values(),
        key=lambda item: (
            getattr(item, "repo", ""),
            (getattr(item, "method", "") or ""),
            _normalize_identity_path(getattr(item, "path", None)),
            getattr(item, "file", ""),
        ),
    )


def routes_command(
    repo: Annotated[list[str] | None, typer.Option("--repo")] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help=(
                "Model selection:\n"
                "  --model ollama:llama3.1:8b\n"
                "  --model openai:gpt-4.1-mini\n"
                "  --model anthropic:claude-3-5-sonnet-latest\n\n"
                "Environment defaults:\n"
                "  SYDES_LLM_PROVIDER=openai\n"
                "  SYDES_LLM_MODEL=gpt-4.1-mini\n"
                "  OPENAI_API_KEY=...\n\n"
                "  SYDES_LLM_PROVIDER=anthropic\n"
                "  SYDES_LLM_MODEL=claude-3-5-sonnet-latest\n"
                "  ANTHROPIC_API_KEY=..."
            ),
        ),
    ] = None,
    output_format: Annotated[
        Literal["terminal", "json"], typer.Option("--format")
    ] = "terminal",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    llm_policy: Annotated[
        Literal["auto", "always", "never"],
        typer.Option("--llm-policy"),
    ] = "auto",
    model_timeout: Annotated[
        float | None,
        typer.Option("--model-timeout"),
    ] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache")] = False,
    allow_partial: Annotated[bool, typer.Option("--allow-partial")] = False,
) -> None:
    """Discover routes for input repositories using shallow+LLM pipeline."""
    try:
        repos = parse_repo_specs(repo or [])
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc

    workspace_id = compute_workspace_id(repos)
    run_id = create_run_id()

    if no_cache:
        cache_status = None
        cached = None
    else:
        cache_status, cached = load_cache_bundle(
            workspace_id=workspace_id,
            repos=repos,
            llm_policy=llm_policy,
            model_fingerprint=model,
        )

    if not no_cache and cache_status is not None and cache_status.hit and cached is not None:
        try:
            routes_payload = cached.artifacts.get("routes_discovery", {})
            result = RoutesResult.model_validate(routes_payload.get("result", {}))
        except Exception:  # noqa: BLE001
            cached = None
            cache_status = None
        else:
            result.notes.append("discovery_cache=hit")
            result.notes.append(
                "discovery_cache_reused_artifacts="
                + ",".join(name for name in CACHEABLE_ARTIFACT_NAMES if name in cached.artifacts)
            )
            for name in CACHEABLE_ARTIFACT_NAMES:
                payload = cached.artifacts.get(name)
                if payload is None:
                    continue
                try:
                    path = save_run_artifact(
                        workspace_id=workspace_id,
                        run_id=run_id,
                        artifact_name=name,
                        payload=payload,
                    )
                    if name == "routes_discovery":
                        result.notes.append(f"Saved discovery artifact: {path}")
                    elif name == "repo_map":
                        result.notes.append(f"Saved repo map artifact: {path}")
                    elif name == "route_index":
                        result.notes.append(f"Saved route index artifact: {path}")
                    elif name == "route_graph_facts":
                        result.notes.append(f"Saved route graph facts artifact: {path}")
                    elif name == "discovery_coverage":
                        result.notes.append(f"Saved discovery coverage artifact: {path}")
                    elif name == "routing_pattern_plan":
                        result.notes.append(f"Saved routing pattern plan artifact: {path}")
                    elif name == "routing_pattern_execution":
                        result.notes.append(f"Saved routing pattern execution artifact: {path}")
                except OSError:
                    continue

            rendered = (
                render_routes_json(result)
                if output_format == "json"
                else render_routes_terminal(result)
            )
            typer.echo(rendered)
            if output is not None:
                try:
                    _write_command_output(output, rendered, output_format=output_format)
                except (OSError, ValueError) as exc:
                    typer.echo(str(exc))
                    raise typer.Exit(code=1) from exc
            return

    validation = None
    if llm_policy == "always":
        validation = validate_llm_available(model_spec=model)
        if not validation.ok:
            message = validation.reason or "LLM preflight failed."
            if output_format == "json":
                payload = {
                    "ok": False,
                    "error": {
                        "provider": validation.provider,
                        "model": validation.model,
                        "base_url": validation.base_url,
                        "message": message,
                        "available_models": list(validation.available_models),
                    },
                }
                rendered = json.dumps(payload, indent=2)
                typer.echo(rendered)
                if output is not None:
                    try:
                        _write_command_output(output, rendered, output_format=output_format)
                    except (OSError, ValueError) as exc:
                        typer.echo(str(exc))
                        raise typer.Exit(code=1) from exc
                raise typer.Exit(code=1)
            typer.echo(f"LLM validation failed: {message}")
            raise typer.Exit(code=1)

    try:
        discover_kwargs: dict[str, object] = {
            "model_spec": model,
            "strict_llm": not allow_partial,
        }
        if llm_policy != "auto":
            discover_kwargs["llm_policy"] = llm_policy
        if model_timeout is not None:
            discover_kwargs["model_timeout_seconds"] = model_timeout
        result = discover_endpoints(repos, **discover_kwargs)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc
    except LLMClientError as exc:
        message = str(exc)
        if output_format == "json":
            payload = {
                "ok": False,
                "error": {
                    "provider": validation.provider if validation is not None else None,
                    "model": validation.model if validation is not None else None,
                    "base_url": validation.base_url if validation is not None else None,
                    "message": message,
                    "available_models": list(validation.available_models) if validation is not None else [],
                },
            }
            rendered = json.dumps(payload, indent=2)
            typer.echo(rendered)
            if output is not None:
                try:
                    _write_command_output(output, rendered, output_format=output_format)
                except (OSError, ValueError) as exc:
                    typer.echo(str(exc))
                    raise typer.Exit(code=1) from exc
            raise typer.Exit(code=1)
        typer.echo(f"LLM discovery failed: {message}")
        raise typer.Exit(code=1)

    if no_cache:
        cache_note = "discovery_cache=disabled"
    elif cache_status is not None and not cache_status.hit:
        cache_note = (
            f"discovery_cache=miss reason={cache_status.reason}"
            + (f" changed_files={cache_status.changed_files}" if cache_status.changed_files else "")
        )
    else:
        cache_note = "discovery_cache=miss reason=cache_bypass"
    result.notes.append(cache_note)

    artifact_payload = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "repo_inputs": [item.model_dump() for item in repos],
        "result": result.model_dump(),
    }

    try:
        artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="routes_discovery",
            payload=artifact_payload,
        )
        result.notes.append(f"Saved discovery artifact: {artifact_path}")
    except OSError as exc:
        result.notes.append(f"Could not save discovery artifact: {exc}")

    repo_map_batch: dict | None = None
    try:
        repo_map_batch = build_repo_map_batch(repos)
        repo_map_payload = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "repo_inputs": [item.model_dump() for item in repos],
            "map": repo_map_batch,
        }
        repo_map_artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="repo_map",
            payload=repo_map_payload,
        )
        result.notes.append(f"Saved repo map artifact: {repo_map_artifact_path}")
    except OSError as exc:
        result.notes.append(f"Could not save repo map artifact: {exc}")

    route_index_batch: dict | None = None
    try:
        route_index_payload = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "repo_inputs": [item.model_dump() for item in repos],
            "index": build_route_index_batch(repos, repo_map_batch=repo_map_batch),
        }
        route_index_batch = route_index_payload["index"]
        route_index_artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="route_index",
            payload=route_index_payload,
        )
        result.notes.append(f"Saved route index artifact: {route_index_artifact_path}")
    except OSError as exc:
        result.notes.append(f"Could not save route index artifact: {exc}")

    route_graph_facts: dict = {"repos": []}
    try:
        route_graph_facts = build_route_graph_facts_batch(repos, route_index_batch=route_index_batch)
        route_graph_facts.pop("_repo_endpoint_candidates", None)
        route_graph_payload = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "repo_inputs": [item.model_dump() for item in repos],
            "graph_facts": route_graph_facts,
        }
        route_graph_artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="route_graph_facts",
            payload=route_graph_payload,
        )
        result.notes.append(f"Saved route graph facts artifact: {route_graph_artifact_path}")
    except OSError as exc:
        result.notes.append(f"Could not save route graph facts artifact: {exc}")

    coverage_by_repo: dict[str, dict] = {}
    route_index_repos = _repo_index_by_name(route_index_batch, "repos")
    route_graph_repos = _repo_index_by_name(route_graph_facts, "repos")
    repo_map_repos = _repo_index_by_name(repo_map_batch, "repos")
    try:
        for repo_ref in repos:
            repo_name = repo_ref.name
            route_index_summary = route_index_repos.get(repo_name, {}).get("summary", {})
            route_graph_summary = route_graph_repos.get(repo_name, {}).get("summary", {})
            deterministic_route_count = _extract_repo_note_int(result.notes, repo_name, "deterministic_routes_found")
            truncated_files = _extract_repo_note_int(result.notes, repo_name, "deterministic_scan_truncated_files")
            coverage_by_repo[repo_name] = evaluate_discovery_coverage(
                route_index_summary=route_index_summary,
                route_graph_summary=route_graph_summary,
                deterministic_route_count=deterministic_route_count,
                deterministic_scan_truncated_files=truncated_files,
            )

        coverage_payload = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "repo_inputs": [item.model_dump() for item in repos],
            "coverage": coverage_by_repo,
        }
        coverage_artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="discovery_coverage",
            payload=coverage_payload,
        )
        result.notes.append(f"Saved discovery coverage artifact: {coverage_artifact_path}")
    except OSError as exc:
        result.notes.append(f"Could not save discovery coverage artifact: {exc}")

    planner_llm_client: LLMClient | None = None
    planner_client_init_error: str | None = None
    plans_by_repo: dict[str, dict] = {}
    for repo_ref in repos:
        repo_name = repo_ref.name
        coverage = coverage_by_repo.get(repo_name, {"label": "unknown", "score": 0.0, "reasons": []})
        coverage_label = str(coverage.get("label") or "unknown")
        should_run_planner = False
        skip_reason = ""
        if llm_policy == "never":
            skip_reason = "policy_never"
        elif llm_policy == "always":
            should_run_planner = True
        elif coverage_label in {"weak", "unknown"}:
            should_run_planner = True
        else:
            skip_reason = f"coverage_{coverage_label}"

        if not should_run_planner:
            plans_by_repo[repo_name] = {
                "skipped": True,
                "reason": skip_reason,
                "coverage_label": coverage_label,
            }
            result.notes.append(f"{repo_name}: routing_pattern_planner=skipped reason={skip_reason}")
            continue

        if planner_llm_client is None and planner_client_init_error is None:
            try:
                planner_llm_client = create_default_llm_client(
                    model_spec=model,
                    timeout_seconds_override=model_timeout,
                )
            except LLMClientError as exc:
                planner_client_init_error = str(exc)

        if planner_client_init_error is not None or planner_llm_client is None:
            reason = planner_client_init_error or "LLM client unavailable"
            plans_by_repo[repo_name] = {"failed": True, "reason": reason}
            result.notes.append(f"{repo_name}: routing_pattern_planner=failed reason={reason}")
            continue

        planner_input = build_routing_pattern_planner_input(
            repo_name=repo_name,
            repo_map_repo=repo_map_repos.get(repo_name),
            route_index_repo=route_index_repos.get(repo_name),
            route_graph_repo=route_graph_repos.get(repo_name),
            coverage=coverage,
        )
        try:
            plan = run_routing_pattern_planner(
                repo_name=repo_name,
                planner_input=planner_input,
                llm_client=planner_llm_client,
            )
            plans_by_repo[repo_name] = plan
            result.notes.append(
                f"{repo_name}: routing_pattern_planner=ran confidence={plan.get('confidence')} convention={plan.get('routing_convention')}"
            )
        except (LLMClientError, ValueError) as exc:
            plans_by_repo[repo_name] = {"failed": True, "reason": str(exc)}
            result.notes.append(f"{repo_name}: routing_pattern_planner=failed reason={exc}")

    execution_by_repo: dict[str, dict] = {}
    plan_routes: list = []
    for repo_ref in repos:
        repo_name = repo_ref.name
        plan = plans_by_repo.get(repo_name)
        if not isinstance(plan, dict) or plan.get("skipped") or plan.get("failed"):
            continue
        execution = execute_routing_pattern_plan(
            repo_name=repo_name,
            plan=plan,
            route_graph_repo=route_graph_repos.get(repo_name),
        )
        execution_by_repo[repo_name] = {
            "plan_applied": execution.get("plan_applied", False),
            "routes_added": execution.get("routes_added", 0),
            "mount_edges_used": execution.get("mount_edges_used", 0),
            "unresolved_mounts": execution.get("unresolved_mounts", 0),
            "warnings": execution.get("warnings", []),
        }
        if execution.get("plan_applied"):
            plan_routes.extend(execution.get("routes", []))
            result.notes.append(f"{repo_name}: Applied routing pattern plan: routes_added={execution.get('routes_added', 0)}")
        for warning in execution.get("warnings", []):
            result.notes.append(f"{repo_name}: routing_pattern_execution_warning={warning}")

    if plan_routes:
        result.routes = _merge_route_lists(result.routes, plan_routes)

    try:
        routing_pattern_payload = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "repo_inputs": [item.model_dump() for item in repos],
            "plans": plans_by_repo,
        }
        routing_pattern_artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="routing_pattern_plan",
            payload=routing_pattern_payload,
        )
        result.notes.append(f"Saved routing pattern plan artifact: {routing_pattern_artifact_path}")
    except OSError as exc:
        result.notes.append(f"Could not save routing pattern plan artifact: {exc}")

    try:
        execution_payload = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "repo_inputs": [item.model_dump() for item in repos],
            "execution": execution_by_repo,
        }
        execution_artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="routing_pattern_execution",
            payload=execution_payload,
        )
        result.notes.append(f"Saved routing pattern execution artifact: {execution_artifact_path}")
    except OSError as exc:
        result.notes.append(f"Could not save routing pattern execution artifact: {exc}")

    if not no_cache:
        try:
            cache_artifacts = {
                "routes_discovery": artifact_payload,
                "repo_map": repo_map_payload if "repo_map_payload" in locals() else None,
                "route_index": route_index_payload if "route_index_payload" in locals() else None,
                "route_graph_facts": route_graph_payload if "route_graph_payload" in locals() else None,
                "discovery_coverage": coverage_payload if "coverage_payload" in locals() else None,
                "routing_pattern_plan": routing_pattern_payload if "routing_pattern_payload" in locals() else None,
                "routing_pattern_execution": execution_payload if "execution_payload" in locals() else None,
            }
            cache_artifacts = {k: v for k, v in cache_artifacts.items() if v is not None}
            save_cache_bundle(
                workspace_id=workspace_id,
                repos=repos,
                llm_policy=llm_policy,
                model_fingerprint=model,
                artifacts=cache_artifacts,
            )
        except OSError as exc:
            result.notes.append(f"Could not update discovery cache: {exc}")

    rendered = (
        render_routes_json(result)
        if output_format == "json"
        else render_routes_terminal(result)
    )

    typer.echo(rendered)
    if output is not None:
        try:
            _write_command_output(output, rendered, output_format=output_format)
        except (OSError, ValueError) as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
