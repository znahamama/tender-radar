"""Score a harness run and write it as Markdown and JSON (the same data in both)."""

from __future__ import annotations

import json
from pathlib import Path

from . import scoring
from .harness import call_stats
from .scoring import ERROR_TYPES, FIELDS, STATUSES, SYSTEMS

SYSTEM_NAMES = {"llm": "LLM", "baseline": "Baseline"}
STATUS_SHORT = {"blocked": "blocked", "check_solicitation": "check_sol.",
                "no_blockers_stated": "no_blockers"}
MAX_QUOTE = 300


def score(run: dict) -> dict:
    """Add every computed table to the harness output under "scores"."""
    cases = run["cases"]
    sure = [c for c in cases if not c["unsure"]]
    return {**run, "scores": {
        "fields": scoring.field_scores(cases),
        "fields_excluding_unsure": scoring.field_scores(sure),
        "status": {s: scoring.confusion(cases, s) for s in SYSTEMS},
        "status_excluding_unsure": {s: scoring.confusion(sure, s) for s in SYSTEMS},
        "consistency": scoring.consistency(cases),
        "grounding": grounding(cases),
        "calls": call_stats(run["calls"]),
        "n": len(cases), "n_unsure": len(cases) - len(sure),
        "unsure_cases": [c["case"] for c in cases if c["unsure"]],
    }}


def grounding(cases: list[dict]) -> dict:
    """Protocol metric 3: ungrounded gate evidence (gate fell back to unknown) and details."""
    out = {}
    for s in SYSTEMS:
        flags = [f for c in cases for f in c["systems"][s]["flags"]]
        out[s] = {"ungrounded_gates": sum(f.startswith("ungrounded:eligibility.") for f in flags),
                  "ungrounded_details": sum(f.startswith("ungrounded_detail:") for f in flags),
                  "failed_extractions": sum(not c["systems"][s]["ok"] for c in cases)}
    return out


# --- Markdown --------------------------------------------------------------------------

def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.0f}%"


def _ci(ci) -> str:
    return "-" if not ci else f"{100 * ci[0]:.0f}–{100 * ci[1]:.0f}%"


def _frac(t: dict) -> str:
    return f"{t['correct']}/{t['n']}"


def _quote(q: str | None) -> str:
    if q is None:
        return "_none_"
    q = " ".join(q.split())
    if len(q) > MAX_QUOTE:
        q = q[:MAX_QUOTE].rstrip() + " […]"
    return "“" + q.replace("|", "\\|") + "”"


def _code(v) -> str:
    return f"`{'unknown' if v is None else v}`"


def render(r: dict) -> str:
    s, p, v = r["scores"], r["protocol"], r["protocol"]["versions"]
    n, n_unsure = s["n"], s["n_unsure"]
    out = [f"# Eval report: {r['split']} split, {r['date']}", ""]

    out += ["## Protocol", "",
            (f"- **Frozen versions:** LLM `{v['llm']['model_id']}` (effort {v['llm']['effort']}, "
            f"max_tokens {v['llm']['max_tokens']}), prompt `{v['prompt_version']}` "
            f"(`{v['prompt_sha256'][:12]}…`), schema `{v['schema_version']}` "
            f"(`{v['schema_sha256'][:12]}…`); keyword baseline `{v['baseline_version']}` "
            f"(`{v['baseline_fingerprint'][:12]}…`); eligibility rules v{v['eligibility_version']} "
            f"(`{v['eligibility_sha256'][:12]}…`). Full hashes in the JSON."),
            (f"- **Cases:** seeded selection (seed {p['seed']}, {p['selected_on']}), "
            f"**n = {n} {r['split']}** cases. Labels: {p['labels']}. "
            f"{n_unsure} marked unsure ({', '.join(s['unsure_cases']) or 'none'})."),
            (f"- **Status:** expected status derived from the gate labels with rules "
            f"v{v['eligibility_version']}, the tender's notice_kind and "
            f"sa_required_from_metadata, for profile “{p['profile']['name']}” "
            f"(SAs: {', '.join(p['profile']['supply_arrangements']) or 'none'}; clearance "
            f"{p['profile']['corporate_clearance']}/{p['profile']['personnel_clearance']})."),
            ("- **Compared after post-validation** (grounding), i.e. what the site shows. The "
            "SA field is the extracted value; the metadata override only enters the status."),
            (f"- **Run:** `{r['run_tag']}`, {r['repeats_requested']} LLM repeat(s) per case; "
            f"commit `{p['git']['commit'][:10]}`{' (uncommitted changes)' if p['git']['dirty'] else ''}."),
            "", (f"> **n is tiny.** With {n} cases one case is {100 // max(n, 1)} points; the 95% "
            "Wilson intervals below are wide and mostly overlap. Use this split to read errors, "
            "not to rank systems."), ""]
    if r["stopped"]:
        out += [f"> **Repeats stopped early:** {r['stopped']}", ""]
    changed = [c["case"] for c in r["cases"] if c["text_changed_since_selection"]]
    if changed:
        out += [(f"> **Text changed since selection:** {', '.join(changed)} (labels were made "
                "on the earlier text)."), ""]

    f_all, f_sure = s["fields"], s["fields_excluding_unsure"]
    n_sure = n - n_unsure
    out += ["## Summary: correct per field", "",
            (f"| Field | LLM | 95% CI | Baseline | 95% CI | LLM excl. unsure (n={n_sure}) "
            f"| Baseline excl. unsure |"),
            "|---|---|---|---|---|---|---|"]
    for name in FIELDS:
        la, ba = f_all["llm"][name], f_all["baseline"][name]
        out.append(f"| {name} | {_frac(la)} | {_ci(la['ci95'])} | {_frac(ba)} | "
                   f"{_ci(ba['ci95'])} | {_frac(f_sure['llm'][name])} | "
                   f"{_frac(f_sure['baseline'][name])} |")
    for label, scores in (("**All fields**", f_all), ("**All, excl. unsure**", f_sure)):
        row = [label]
        for sys in SYSTEMS:
            k = sum(scores[sys][f]["correct"] for f in FIELDS)
            m = sum(scores[sys][f]["n"] for f in FIELDS)
            row.append(f"{k}/{m} ({_pct(k / m if m else None)})")
        out.append(f"| {row[0]} | {row[1]} | | {row[2]} | | | |")
    out += ["", "_Pooled “all fields” counts are not independent (8 fields × the same cases)._",
            ""]

    out += ["## Error types", "",
            ("Missed: the label is known, the system said unknown. Overclaimed: the label is "
            "unknown, the system gave a value (the worst kind: it states something the notice "
            "doesn't say). Criteria_location has no unknown value, so it is only correct or "
            "wrong (the baseline always says see_solicitation)."), ""]
    for sys in SYSTEMS:
        out += [f"**{SYSTEM_NAMES[sys]}**", "",
                "| Field | correct | wrong | missed | overclaimed | system unknown |",
                "|---|---|---|---|---|---|"]
        totals = dict.fromkeys(ERROR_TYPES, 0)
        for name in FIELDS:
            t = f_all[sys][name]
            for e in ERROR_TYPES:
                totals[e] += t[e]
            out.append(f"| {name} | " + " | ".join(str(t[e]) for e in ERROR_TYPES)
                       + f" | {t['system_unknown']}/{t['n']} |")
        out += ["| **total** | " + " | ".join(f"**{totals[e]}**" for e in ERROR_TYPES) + " | |",
                ""]

    out += ["## Status (can-bid decision)", "",
            "Rows: expected (from the labels). Columns: the system's status.", ""]
    for sys in SYSTEMS:
        c = s["status"][sys]
        cs = s["status_excluding_unsure"][sys]
        out += [(f"**{SYSTEM_NAMES[sys]}**: agrees on {c['agree']}/{c['n']} "
                f"(95% CI {_ci(c['ci95'])}); excluding unsure {cs['agree']}/{cs['n']}."), "",
                "| expected \\ got | " + " | ".join(STATUS_SHORT[x] for x in STATUSES) + " |",
                "|---|---|---|---|"]
        for e in STATUSES:
            out.append(f"| {STATUS_SHORT[e]} | "
                       + " | ".join(str(c["table"][e][g]) for g in STATUSES) + " |")
        out += ["", (f"- False blocks (system blocked, labels don't): "
                    f"{', '.join(c['false_blocks']) or 'none'}"),
                (f"- Missed blocks (labels block, system didn't): "
                f"{', '.join(c['missed_blocks']) or 'none'}"), ""]

    con = s["consistency"]
    out += ["## Consistency (LLM, run to run)", ""]
    if not con["cases"]:
        out += ["No repeats in this run.", ""]
    else:
        runs = sorted(set(con["runs_per_case"].values()))
        out += [(f"Runs per case: the cached extraction + the repeats = "
                f"{'/'.join(map(str, runs))}. Same answer in every run:"), "",
                "| Field | stable | cases that flip |", "|---|---|---|"]
        for name, f in con["fields"].items():
            flips = ", ".join(x["case"] for x in f["flips"]) or "-"
            out.append(f"| {name} | {f['stable']}/{f['n']} | {flips} |")
        details = [(name, x) for name, f in con["fields"].items() for x in f["flips"]]
        if details:
            out += ["", "Flips (label, then each run: cached first):", ""]
            for name, x in details:
                out.append(f"- {x['case']} {name}: label {_code(x['label'])}; runs "
                           + ", ".join(_code(val) for val in x["values"]))
        out.append("")

    g = s["grounding"]
    out += ["## Grounding", "",
            ("| System | gates reset to unknown (ungrounded evidence) | details dropped "
            "| failed extractions |"), "|---|---|---|---|"]
    for sys in SYSTEMS:
        out.append(f"| {SYSTEM_NAMES[sys]} | {g[sys]['ungrounded_gates']} | "
                   f"{g[sys]['ungrounded_details']} | {g[sys]['failed_extractions']} |")
    out.append("")

    out += ["## Cost, tokens and latency (API calls made by this run)", ""]
    cached = sum(c["systems"]["llm"]["outcome"] == "cache_hit" for c in r["cases"])
    if s["calls"]:
        out += [("| Purpose | calls (ok) | tokens in | tokens out | cost USD | latency median "
                "/ mean / max |"), "|---|---|---|---|---|---|"]
        for purpose, c in s["calls"].items():
            lat = c["latency_ms"]
            est = f", {c['estimated']} estimated" if c["estimated"] else ""
            out.append(f"| {purpose} | {c['calls']} ({c['ok']}{est}) | {c['input_tokens']:,} | "
                       f"{c['output_tokens']:,} | ${c['cost_usd']} | "
                       f"{lat['median'] / 1000:.1f} / {lat['mean'] / 1000:.1f} / "
                       f"{lat['max'] / 1000:.1f} s |")
        out.append("")
    else:
        out += ["No API calls.", ""]
    out += [(f"`extract`: the cached extraction, made now where missing ({cached} of {n} were "
            "already cached from earlier runs; their cost is not counted here). `eval`: the "
            "repeats. The baseline makes no API call and costs nothing."), ""]

    out += ["## Per-case disagreements", "",
            ("Every field where the LLM or the baseline differs from the label, with the "
            "evidence quotes. ⚠ = labeller marked the case unsure."), ""]
    for c in r["cases"]:
        rows = scoring.disagreements(c)
        st = {sys: c["systems"][sys]["status"] for sys in SYSTEMS}
        head = f"### {c['case']} · `{c['reference']}` · {c['notice_kind']}"
        if c["unsure"]:
            head += " ⚠ unsure"
        out += [head, ""]
        if c["unsure"]:
            out += [f"> Labeller's note: {c['notes']}", ""]
        out.append(f"Status: expected **{c['label']['status']}**, LLM **{st['llm']}**, "
                   f"baseline **{st['baseline']}**."
                   + ("" if c["systems"]["llm"]["ok"]
                      else f" LLM extraction failed: {c['systems']['llm']['error']}"))
        out.append("")
        if not rows:
            out += ["All 8 fields match the label for both systems.", ""]
            continue
        for row in rows:
            out.append(f"- **{row['field']}**: label {_code(row['label']['value'])} · LLM "
                       f"{_code(row['llm']['value'])} ({row['llm']['kind']}) · baseline "
                       f"{_code(row['baseline']['value'])} ({row['baseline']['kind']})")
            if row["field"] == "criteria_location":
                continue
            gate = FIELDS[row["field"]][0]
            for who, ev in (("label", row["label"]["evidence"]),
                            ("LLM", row["llm"]["evidence"]),
                            ("baseline", row["baseline"]["evidence"])):
                note = ""
                if who != "label":
                    sys = "llm" if who == "LLM" else "baseline"
                    if f"ungrounded:eligibility.{gate}" in c["systems"][sys]["flags"]:
                        note = " (its evidence was not found in the notice, so the gate was " \
                               "reset to unknown)"
                out.append(f"  - {who}: {_quote(ev)}{note}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write(r: dict, reports_dir: Path) -> tuple[Path, Path]:
    """eval/reports/<split>-<date>.md and .json; a later run on the same day gets -2, -3..."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{r['split']}-{r['date']}"
    i = 1
    while (reports_dir / f"{stem}.md").exists() or (reports_dir / f"{stem}.json").exists():
        i += 1
        stem = f"{r['split']}-{r['date']}-{i}"
    md, js = reports_dir / f"{stem}.md", reports_dir / f"{stem}.json"
    md.write_text(render(r), encoding="utf-8")
    js.write_text(json.dumps(r, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return md, js
