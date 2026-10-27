"""Pre-registered metrics for the allocation experiment (A1, A2 control; C1, C2 two_round)."""
import collections
import glob
import json
import os
import re
import sys

R = os.path.expanduser("~/StudioProjects/sydes/benchmarks/aacr/results")
ROOT = sys.argv[1] if len(sys.argv) > 1 else R
ARMS = {
    "A1": "alloc-A1-snapshot-c54d97e-first11-2026-10-26",
    "A2": "alloc-A2-replay-sequential-c54d97e-first11-2026-10-26",
    "C1": "alloc-C1-replay-two_round-c54d97e-first11-2026-10-26",
    "C2": "alloc-C2-replay-two_round-c54d97e-first11-2026-10-26",
}
CASES = json.load(open(os.path.expanduser(
    "~/StudioProjects/sydes/benchmarks/aacr/baselines/2026-10-25-sydes-0.4.0-dev-laptop-first11/cases-completed.json")))["cases"]


def sid(c):
    return c["instance_id"].replace("/", "__")


def load(run):
    d = f"{ROOT}/{run}"
    tele = {sid(c): json.load(open(f"{d}/telemetry/{sid(c)}.json")) for c in CASES if os.path.exists(f"{d}/telemetry/{sid(c)}.json")}
    inv = {}
    for c in CASES:
        p = f"{d}/sydes/{sid(c)}.investigation.json"
        if os.path.exists(p):
            inv[sid(c)] = json.load(open(p))
    m = glob.glob(f"{d}/eval-file_level/metrics_ocr_*.json")
    ev = {e["instance_id"]: e for e in json.load(open(m[0]))["eval_res"]} if m else {}
    return tele, inv, ev


data = {a: load(r) for a, r in ARMS.items()}
valid = [sid(c) for c in CASES if all(c_ok for c_ok in
         (data[a][0].get(sid(c), {}).get("invalid_reasons") == [] for a in ARMS))]
invalid = [sid(c) for c in CASES if sid(c) not in valid]
snap = {s: json.load(open(f"{ROOT}/{ARMS['A1']}/sydes/{s}.snapshot.json")) for s in valid}

# starved targets, from A1's own trace (zero lookups, or UNKNOWN at the sequential cap of 4)
targets = []
for s in valid:
    a = data["A1"][1][s]
    per = collections.Counter(l["hypothesis"] for l in a["lookups"])
    for o in a["outcomes"]:
        n = per.get(o["hypothesis"], 0)
        if n == 0 or (o["status"] == "UNKNOWN" and n >= 4):
            targets.append((s, o["hypothesis"], "zero" if n == 0 else "cap"))

WORD = re.compile(r"`([^`]{3,80})`|\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+|[a-z]+[A-Z][A-Za-z0-9]+|[A-Z][a-z]+[A-Z][A-Za-z0-9]+)\b")


def terms(texts):
    out = set()
    for t in texts:
        for m in WORD.finditer(t):
            out.add((m.group(1) or m.group(2)).strip())
    return {t for t in out if len(t) >= 4}


arms = {}
for a in ARMS:
    tele, inv, ev = data[a]
    H = C = Rf = U = L = zero = finds = match = 0
    calls = tin = tout = 0
    per_case = {}
    status = {}
    rounds = collections.Counter()
    missing_hit = [0, 0]
    for s in valid:
        art = inv[s]
        per = collections.Counter(l["hypothesis"] for l in art["lookups"])
        hs = art["hypotheses"]
        H += len(hs); C += len(art["confirmed"]); Rf += len(art["refuted"]); U += len(art["unknown"])
        L += len(art["lookups"]); zero += sum(1 for h in hs if per.get(h["id"], 0) == 0)
        e = ev.get(s.replace("__", "/", 0)) or ev.get(s) or {"generated_comments": []}
        f = len(e["generated_comments"]); mt = sum(1 for g in e["generated_comments"] if g.get("matched_gt_indices"))
        finds += f; match += mt
        u = art["usage"]
        fu = snap[s]["finder_usage"] if a == "A1" else {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        calls += u["llm_calls"] - fu["llm_calls"]; tin += u["prompt_tokens"] - fu["prompt_tokens"]; tout += u["completion_tokens"] - fu["completion_tokens"]
        for o in art["outcomes"]:
            status[(s, o["hypothesis"])] = (o["status"], per.get(o["hypothesis"], 0))
            rs = o.get("rounds") or []
            if len(rs) == 2:
                r2 = rs[1]
                rounds["round2"] += 1
                rounds["verified_again"] += r2["verified_again"]
                rounds["status_changed"] += r2["status_changed"]
                if r2["status_changed"]:
                    rounds["changed_with_new_nonempty"] += bool(r2["new_nonempty_lookups"])
                    rounds[f"change {rs[0]['status']}->{r2['status']}"] += 1
                if r2["verified_again"] and not r2["new_nonempty_lookups"]:
                    rounds["reverified_on_empty_only"] += 1
                want = terms(rs[0]["missing"])
                got = " ".join(json.dumps(l["args"]) + " " + (l.get("result_excerpt") or "")
                               for l in art["lookups"] if l["id"] in r2["lookups"])
                if want and r2["lookups"]:
                    missing_hit[1] += 1
                    missing_hit[0] += any(t in got for t in want)
        per_case[s] = dict(H=len(hs), L=len(art["lookups"]), C=len(art["confirmed"]), R=len(art["refuted"]),
                           U=len(art["unknown"]), zero=sum(1 for h in hs if per.get(h["id"], 0) == 0),
                           per={h["id"]: per.get(h["id"], 0) for h in hs}, findings=f, matched=mt,
                           calls=u["llm_calls"] - fu["llm_calls"], seconds=tele[s]["wall_seconds"])
    n = len(valid)
    refs = sum(len((ev.get(s) or {"comments": []})["comments"]) for s in valid)
    p = match / finds if finds else 0.0
    r = match / refs if refs else 0.0
    arms[a] = dict(H=H, C=C, R=Rf, U=U, decided=C + Rf, decided_frac=round((C + Rf) / H, 3) if H else 0,
                   L=L, zero=zero, findings=finds, matched=match, refs=refs, P=round(p, 3), Rc=round(r, 3),
                   balanced=round(2 * p * r / (p + r), 3) if p + r else 0.0,
                   inv_ver_calls_per_pr=round(calls / n, 1), inv_ver_in_per_pr=round(tin / n), inv_ver_out_per_pr=round(tout / n),
                   seconds_per_pr=round(sum(tele[s]["wall_seconds"] for s in valid) / n, 1),
                   rounds=dict(rounds), round2_read_missing_term=missing_hit, per_case=per_case,
                   targets={f"{s}:{h}({k})": status.get((s, h)) for s, h, k in targets},
                   unmatched_confirmed=[(s, g.get("path"), g.get("content", "")[:160]) for s in valid
                                        for g in (ev.get(s) or {"generated_comments": []})["generated_comments"]
                                        if not g.get("matched_gt_indices")])

out = {"valid": valid, "invalid": invalid, "targets": targets, "arms": arms}
mean_a = (arms["A1"]["decided_frac"] + arms["A2"]["decided_frac"]) / 2
noise = abs(arms["A1"]["decided_frac"] - arms["A2"]["decided_frac"])
out["decision_inputs"] = {"mean_A_decided_frac": round(mean_a, 3), "noise_A1_A2": round(noise, 3),
                          "C1_minus_meanA": round(arms["C1"]["decided_frac"] - mean_a, 3),
                          "C2_minus_meanA": round(arms["C2"]["decided_frac"] - mean_a, 3),
                          "calls_ratio_C_over_A": round(((arms["C1"]["inv_ver_calls_per_pr"] + arms["C2"]["inv_ver_calls_per_pr"]) / 2)
                                                        / ((arms["A1"]["inv_ver_calls_per_pr"] + arms["A2"]["inv_ver_calls_per_pr"]) / 2), 3)}
json.dump(out, open(f"{ROOT}/_alloc_analysis.json", "w"), indent=1)
for a, v in arms.items():
    print(a, {k: v[k] for k in ("H", "C", "R", "U", "decided", "decided_frac", "L", "zero", "findings", "matched", "refs", "P", "Rc", "balanced",
                                "inv_ver_calls_per_pr", "inv_ver_in_per_pr", "inv_ver_out_per_pr", "seconds_per_pr", "rounds", "round2_read_missing_term")})
print("valid", len(valid), "invalid", invalid)
print("decision", out["decision_inputs"])
