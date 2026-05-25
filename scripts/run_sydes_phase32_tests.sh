#!/usr/bin/env bash

set +e

TS="$(date +"%Y%m%d_%H%M%S")"
LOG="/tmp/sydes_phase32_test_${TS}.log"

SYDES_REPO="/Users/ksnaik/StudioProjects/sydes"
SYDES_VSCODE_EXT="/Users/ksnaik/StudioProjects/sydes-vscode/extension"

WORKLENZ="/Users/ksnaik/sample_repos/worklenz"
FLASK="/Users/ksnaik/sample_repos/flask-sample-app"
FASTAPI="/Users/ksnaik/sample_repos/SimpleFastPyAPI"
STRESS100="/Users/ksnaik/sample_repos/sydes-stress-fastapi-100"
STRESS_MULTI="/Users/ksnaik/sample_repos/sydes-stress-microservices"

echo "Logging to: $LOG"
echo "Logging to: $LOG" > "$LOG"

run_cmd() {
  local title="$1"
  shift

  {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "$title"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "CMD: $*"
    echo "TIME: $(date)"
    echo ""
  } | tee -a "$LOG"

  START=$(date +%s)

  "$@" 2>&1 | tee -a "$LOG"
  STATUS=${PIPESTATUS[0]}

  END=$(date +%s)
  ELAPSED=$((END - START))

  {
    echo ""
    echo "EXIT_CODE: $STATUS"
    echo "ELAPSED_SECONDS: $ELAPSED"
    echo "FINISHED: $(date)"
  } | tee -a "$LOG"

  return 0
}

run_shell() {
  local title="$1"
  local cmd="$2"

  {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "$title"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "CMD: $cmd"
    echo "TIME: $(date)"
    echo ""
  } | tee -a "$LOG"

  START=$(date +%s)

  bash -lc "$cmd" 2>&1 | tee -a "$LOG"
  STATUS=${PIPESTATUS[0]}

  END=$(date +%s)
  ELAPSED=$((END - START))

  {
    echo ""
    echo "EXIT_CODE: $STATUS"
    echo "ELAPSED_SECONDS: $ELAPSED"
    echo "FINISHED: $(date)"
  } | tee -a "$LOG"

  return 0
}

echo "" | tee -a "$LOG"
echo "Sydes Phase 32 Test Run" | tee -a "$LOG"
echo "Started: $(date)" | tee -a "$LOG"
echo "Machine: $(hostname)" | tee -a "$LOG"
echo "Log: $LOG" | tee -a "$LOG"

run_shell "Environment check" "
cd '$SYDES_REPO' &&
pwd &&
uv --version &&
python --version &&
which jq &&
git status --short
"

run_shell "Sydes CLI help: routes" "
cd '$SYDES_REPO' &&
uv run sydes routes --help
"

run_shell "Run main Sydes pytest" "
cd '$SYDES_REPO' &&
uv run pytest
"

run_shell "Worklenz route discovery summary: auto policy" "
cd '$SYDES_REPO' &&
uv run sydes routes \
  --repo worklenz='$WORKLENZ' \
  --llm-policy auto \
  --format json | jq '{count: (.routes | length), notes}'
"

run_shell "Worklenz route discovery summary: no cache" "
cd '$SYDES_REPO' &&
uv run sydes routes \
  --repo worklenz='$WORKLENZ' \
  --llm-policy auto \
  --no-cache \
  --format json | jq '{count: (.routes | length), notes}'
"

run_shell "Worklenz route discovery summary: second cached run" "
cd '$SYDES_REPO' &&
uv run sydes routes \
  --repo worklenz='$WORKLENZ' \
  --llm-policy auto \
  --format json | jq '{count: (.routes | length), notes}'
"

run_shell "Worklenz tasks routes sample" "
cd '$SYDES_REPO' &&
uv run sydes routes \
  --repo worklenz='$WORKLENZ' \
  --llm-policy auto \
  --format json | jq '.routes[] | select(.path | contains(\"/api/v1/tasks\")) | {method,path,file,handler,status,confidence}' | head -120
"

run_shell "Worklenz admin-center routes sample" "
cd '$SYDES_REPO' &&
uv run sydes routes \
  --repo worklenz='$WORKLENZ' \
  --llm-policy auto \
  --format json | jq '.routes[] | select(.path | contains(\"/api/v1/admin-center\")) | {method,path,file,handler,status,confidence}' | head -120
"

run_shell "Worklenz route kind/path distribution" "
cd '$SYDES_REPO' &&
uv run sydes routes \
  --repo worklenz='$WORKLENZ' \
  --llm-policy auto \
  --format json | jq '
    {
      total: (.routes | length),
      by_method: (.routes | group_by(.method) | map({method: .[0].method, count: length})),
      sample_paths: [.routes[0:20][] | {method,path,file,handler,status}]
    }
  '
"

run_shell "Find latest Worklenz artifact dir" "
ls -td /Users/ksnaik/.sydes/workspaces/*/artifacts/* 2>/dev/null | head -20
"

run_shell "Inspect latest Worklenz-looking artifacts if present" "
LATEST=\$(ls -td /Users/ksnaik/.sydes/workspaces/*/artifacts/* 2>/dev/null | head -1)
echo \"LATEST=\$LATEST\"
ls -la \"\$LATEST\" || true

echo ''
echo 'repo_map summary:'
cat \"\$LATEST/repo_map.json\" 2>/dev/null | jq '{candidate_backend_dirs, candidate_route_dirs, candidate_controller_dirs, entrypoint_candidates, ignored_dirs, summary}' || true

echo ''
echo 'route_index summary:'
cat \"\$LATEST/route_index.json\" 2>/dev/null | jq '.summary' || true

echo ''
echo 'route_graph_facts summary:'
cat \"\$LATEST/route_graph_facts.json\" 2>/dev/null | jq '.summary' || true

echo ''
echo 'discovery_coverage:'
cat \"\$LATEST/discovery_coverage.json\" 2>/dev/null | jq '{label, score, signals, reasons}' || true

echo ''
echo 'routing_pattern_plan:'
cat \"\$LATEST/routing_pattern_plan.json\" 2>/dev/null | jq '{framework_family, routing_convention, confidence, recommended_next_action}' || true

echo ''
echo 'routing_pattern_execution:'
cat \"\$LATEST/routing_pattern_execution.json\" 2>/dev/null | jq '.' || true
"

run_shell "Flask regression route count" "
cd '$SYDES_REPO' &&
uv run sydes routes \
  --repo flask='$FLASK' \
  --llm-policy auto \
  --format json | jq '{count: (.routes | length), routes: [.routes[] | {method,path,file,handler,status}]}'
"

run_shell "Flask POST /items trace regression" "
cd '$SYDES_REPO' &&
uv run sydes trace '/items' \
  --method POST \
  --repo flask='$FLASK' \
  --model ollama:llama3.1:latest
"

run_shell "FastAPI regression route count" "
cd '$SYDES_REPO' &&
uv run sydes routes \
  --repo SimpleFastPyAPI='$FASTAPI' \
  --llm-policy auto \
  --format json | jq '{count: (.routes | length), routes: [.routes[] | {method,path,file,handler,status}]}'
"

run_shell "FastAPI POST /users trace regression" "
cd '$SYDES_REPO' &&
uv run sydes trace '/users' \
  --method POST \
  --repo SimpleFastPyAPI='$FASTAPI' \
  --model ollama:llama3.1:latest
"

run_shell "Synthetic 100 route regression" "
cd '$SYDES_REPO' &&
uv run sydes routes \
  --repo stress='$STRESS100' \
  --llm-policy auto \
  --format json | jq '{count: (.routes | length), notes}'
"

run_shell "Synthetic 4-service 200 route regression" "
cd '$SYDES_REPO' &&
uv run sydes routes \
  --repo service1='$STRESS_MULTI/service1' \
  --repo service2='$STRESS_MULTI/service2' \
  --repo service3='$STRESS_MULTI/service3' \
  --repo service4='$STRESS_MULTI/service4' \
  --llm-policy auto \
  --format json | jq '{count: (.routes | length), notes}'
"

run_shell "Build Sydes binary" "
cd '$SYDES_REPO' &&
uv run python scripts/build_binary.py
"

run_shell "Export Sydes binary to VS Code extension" "
cd '$SYDES_REPO' &&
uv run python scripts/export_vscode_engine.py \
  --target '$SYDES_VSCODE_EXT/bundled-engine/bin'
"

run_shell "Verify bundled binary basic route scan" "
'$SYDES_VSCODE_EXT/bundled-engine/bin/darwin-arm64/sydes' routes \
  --repo flask='$FLASK' \
  --llm-policy auto
"

run_shell "VS Code extension compile" "
cd '$SYDES_VSCODE_EXT' &&
npm run compile
"

echo "" | tee -a "$LOG"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" | tee -a "$LOG"
echo "DONE" | tee -a "$LOG"
echo "Log file: $LOG" | tee -a "$LOG"
echo "Copy this file back to ChatGPT:" | tee -a "$LOG"
echo "$LOG" | tee -a "$LOG"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" | tee -a "$LOG"
