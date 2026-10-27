set -u
cd ~/StudioProjects/sydes
export OPENAI_API_KEY="$(cat ~/.config/diffgenome/openai_api_key)"
A="uv run --no-project --python 3.12 python benchmarks/aacr/adapter/aacr.py"
C=benchmarks/aacr/baselines/2026-10-25-sydes-0.4.0-dev-laptop-first11/cases-completed.json
REF=c54d97e
A1=alloc-A1-snapshot-c54d97e-first11-2026-10-26
$A review --cases $C --run $A1 --sydes-ref $REF --sydes-arg=--investigator --sydes-arg=--investigator-snapshot; echo "A1 review exit=$?"
$A eval --cases $C --run $A1; echo "A1 eval exit=$?"
$A summarize --run $A1 > /dev/null; echo "A1 summarize exit=$?"
for arm in A2:sequential C1:two_round C2:two_round; do
  name=${arm%%:*}; alloc=${arm##*:}
  R=alloc-$name-replay-$alloc-c54d97e-first11-2026-10-26
  $A replay --cases $C --run $R --source-run $A1 --allocation $alloc --sydes-ref $REF; echo "$name replay exit=$?"
  $A eval --cases $C --run $R; echo "$name eval exit=$?"
  $A summarize --run $R > /dev/null; echo "$name summarize exit=$?"
done
