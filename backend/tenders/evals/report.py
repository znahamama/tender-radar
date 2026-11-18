"""Score a harness run and write it as Markdown and JSON (the same data in both)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import scoring
from .harness import call_stats
from .scoring import BASELINE, ERROR_TYPES, FIELDS, STATUSES

STATUS_SHORT = {"blocked": "blocked", "check_solicitation": "check_sol.",
                "no_blockers_stated": "no_blockers"}
MAX_QUOTE = 300


def display(system: str) -> str:
    """llm_v3 -> "LLM v3", baseline -> "Baseline"."""
    if system.startswith("llm_"):
        return f"LLM {system.removeprefix('llm_')}"
    return system.capitalize()


def llm_systems(systems: list[str]) -> list[str]:
    return [s for s in systems if s != BASELINE]


def score(run: dict) -> dict:
    """Add every computed table to the harness output under "scores"."""
    cases, systems = run["cases"], run["systems"]
    llms = llm_systems(systems)
    sure = [c for c in cases if not c["unsure"]]
    return {**run, "scores": {
        "fields": scoring.field_scores(cases, systems),
        "fields_excluding_unsure": scoring.field_scores(sure, systems),
        "status": {s: scoring.confusion(cases, s) for s in systems},
        "status_excluding_unsure": {s: scoring.confusion(sure, s) for s in systems},
        "consistency": {s: scoring.consistency(cases, s) for s in llms},
        "changes": {s: scoring.changes(cases, llms[0], s) for s in llms[1:]},
        "grounding": grounding(cases, systems),
        "calls": call_stats(run["calls"]),
        "n": len(cases), "n_unsure": len(cases) - len(sure),
        "unsure_cases": [c["case"] for c in cases if c["unsure"]],
    }}


def grounding(cases: list[dict], systems: list[str]) -> dict:
    """Protocol metric 3: ungrounded gate evidence (gate fell back to unknown) and details,
    in the primary run of each system."""
    out = {}
    for s in systems:
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


def _row(cells: list) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def _sep(n: int) -> str:
    return "|" + "---|" * n


def _protocol(r: dict) -> list[str]:
    s, p, v = r["scores"], r["protocol"], r["protocol"]["versions"]
    n, n_unsure = s["n"], s["n_unsure"]
    prompts = ", ".join(
        f"`{name}` (`{meta['sha256'][:12]}…`{', the site’s prompt' if meta['site_default'] else ''})"
        for name, meta in v["prompts"].items())
    primaries = []
    for sys in llm_systems(r["systems"]):
        name = sys.removeprefix("llm_")
        if v["prompts"][name]["site_default"]:
            primaries.append(f"{display(sys)}: the cached site extraction")
        else:
            primaries.append(f"{display(sys)}: an eval-only run (EvalExtraction repeat 0, "
                             "never shown on the site)")
    out = ["## Protocol", "",
           (f"- **Versions:** LLM `{v['llm']['model_id']}` (effort {v['llm']['effort']}, "
            f"max_tokens {v['llm']['max_tokens']}) with prompts {prompts}; schema "
            f"`{v['schema_version']}` (`{v['schema_sha256'][:12]}…`); keyword baseline "
            f"`{v['baseline_version']}` (`{v['baseline_fingerprint'][:12]}…`); eligibility rules "
            f"v{v['eligibility_version']} (`{v['eligibility_sha256'][:12]}…`). Full hashes in "
            "the JSON."),
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
           f"- **Primary run per LLM system** (scored below): {'; '.join(primaries)}.",
           (f"- **Run:** `{r['run_tag']}`, {r['repeats_requested']} repeat(s) per case and LLM "
            f"system; commit `{p['git']['commit'][:10]}`"
            f"{' (uncommitted changes)' if p['git']['dirty'] else ''}."),
           "", (f"> **n is tiny.** With {n} cases one case is {100 // max(n, 1)} points; the 95% "
                "Wilson intervals below are wide and mostly overlap. Use this split to read "
                "errors, not to rank systems."), ""]
    if r["stopped"]:
        out += [f"> **Repeats stopped early:** {r['stopped']}", ""]
    changed = [c["case"] for c in r["cases"] if c["text_changed_since_selection"]]
    if changed:
        out += [(f"> **Text changed since selection:** {', '.join(changed)} (labels were made "
                 "on the earlier text)."), ""]
    return out


def _summary(r: dict) -> list[str]:
    s, systems = r["scores"], r["systems"]
    f_all, f_sure = s["fields"], s["fields_excluding_unsure"]
    n_sure = s["n"] - s["n_unsure"]
    head = ["Field"]
    for sys in systems:
        head += [display(sys), "95% CI"]
    head += [f"{display(sys)} excl. unsure (n={n_sure})" for sys in systems]
    out = ["## Summary: correct per field", "", _row(head), _sep(len(head))]
    for name in FIELDS:
        cells = [name]
        for sys in systems:
            t = f_all[sys][name]
            cells += [_frac(t), _ci(t["ci95"])]
        cells += [_frac(f_sure[sys][name]) for sys in systems]
        out.append(_row(cells))
    def total(scores: dict) -> str:
        k = sum(scores[f]["correct"] for f in FIELDS)
        m = sum(scores[f]["n"] for f in FIELDS)
        return f"**{k}/{m}** ({_pct(k / m if m else None)})"

    cells = ["**All fields**"]
    for sys in systems:
        cells += [total(f_all[sys]), ""]
    out.append(_row(cells + [total(f_sure[sys]) for sys in systems]))
    pooled = "_Pooled “all fields” counts are not independent (8 fields × the same cases)._"
    return [*out, "", pooled, ""]


def _error_types(r: dict) -> list[str]:
    f_all = r["scores"]["fields"]
    out = ["## Error types", "",
           ("Missed: the label is known, the system said unknown. Overclaimed: the label is "
            "unknown, the system gave a value (the worst kind: it states something the notice "
            "doesn't say). Criteria_location has no unknown value, so it is only correct or "
            "wrong (the baseline always says see_solicitation)."), ""]
    totals_all = {}
    for sys in r["systems"]:
        out += [f"**{display(sys)}**", "",
                "| Field | correct | wrong | missed | overclaimed | system unknown |",
                "|---|---|---|---|---|---|"]
        totals = dict.fromkeys(ERROR_TYPES, 0)
        for name in FIELDS:
            t = f_all[sys][name]
            for e in ERROR_TYPES:
                totals[e] += t[e]
            out.append(f"| {name} | " + " | ".join(str(t[e]) for e in ERROR_TYPES)
                       + f" | {t['system_unknown']}/{t['n']} |")
        totals_all[sys] = totals
        out += ["| **total** | " + " | ".join(f"**{totals[e]}**" for e in ERROR_TYPES) + " | |",
                ""]
    out += ["Totals side by side:", "", _row(["System", *ERROR_TYPES]), _sep(5)]
    out += [_row([display(sys), *(totals_all[sys][e] for e in ERROR_TYPES)])
            for sys in r["systems"]]
    return out + [""]


def _status(r: dict) -> list[str]:
    s = r["scores"]
    out = ["## Status (can-bid decision)", "",
           "Rows: expected (from the labels). Columns: the system's status.", ""]
    for sys in r["systems"]:
        c, cs = s["status"][sys], s["status_excluding_unsure"][sys]
        out += [(f"**{display(sys)}**: agrees on {c['agree']}/{c['n']} "
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
    return out


def _consistency(r: dict) -> list[str]:
    cons = r["scores"]["consistency"]
    llms = llm_systems(r["systems"])
    out = ["## Consistency (LLM, run to run)", ""]
    if not any(cons[sys]["cases"] for sys in llms):
        return out + ["No repeats in this run.", ""]
    runs = sorted({n for sys in llms for n in cons[sys]["runs_per_case"].values()})
    out += [(f"Runs per case and system: the primary run + the repeats = "
             f"{'/'.join(map(str, runs))}. Cases with the same answer in every run:"), ""]
    head = ["Field", *(f"{display(s)} stable" for s in llms),
            *(f"{display(s)} flips" for s in llms)]
    out += [_row(head), _sep(len(head))]
    for name in (*FIELDS, "status"):
        f = {s: cons[s]["fields"][name] for s in llms}
        out.append(_row([name, *(f"{f[s]['stable']}/{f[s]['n']}" for s in llms),
                         *(", ".join(x["case"] for x in f[s]["flips"]) or "-" for s in llms)]))
    for sys in llms:
        details = [(name, x) for name, f in cons[sys]["fields"].items() for x in f["flips"]]
        if details:
            out += ["", f"{display(sys)} flips (label, then each run: primary first):", ""]
            for name, x in details:
                out.append(f"- {x['case']} {name}: label {_code(x['label'])}; runs "
                           + ", ".join(_code(val) for val in x["values"]))
    return out + [""]


def _changes(r: dict) -> list[str]:
    out = []
    for sys, ch in r["scores"]["changes"].items():
        a, b = display(ch["before"]), display(sys)
        k = ch["fields"]
        out += [f"## {b} vs {a}: primary-run answers that changed", "",
                (f"Fields (gates and criteria_location): **{k['fixed']} fixed**, "
                 f"**{k['broken']} broken**, {k['other']} changed but still not correct. "
                 "⚠ = labeller marked the case unsure."), ""]
        if not ch["rows"]:
            out += ["No answer changed.", ""]
            continue
        out += [_row(["Case", "Field", "Label", a, b, "Change"]), _sep(6)]
        for x in ch["rows"]:
            out.append(_row([x["case"] + (" ⚠" if x["unsure"] else ""), x["field"],
                             _code(x["label"]),
                             f"{_code(x['before']['value'])} ({x['before']['kind']})",
                             f"{_code(x['after']['value'])} ({x['after']['kind']})",
                             x["change"]]))
        out.append("")
    return out


def _grounding(r: dict) -> list[str]:
    g = r["scores"]["grounding"]
    out = ["## Grounding", "", "Primary run of each system.", "",
           ("| System | gates reset to unknown (ungrounded evidence) | details dropped "
            "| failed extractions |"), "|---|---|---|---|"]
    for sys in r["systems"]:
        out.append(f"| {display(sys)} | {g[sys]['ungrounded_gates']} | "
                   f"{g[sys]['ungrounded_details']} | {g[sys]['failed_extractions']} |")
    rep = []
    for sys in llm_systems(r["systems"]):
        flags = [f for c in r["cases"] for x in c["repeats"].get(sys, []) for f in x["flags"]]
        if flags:
            rep.append(f"{display(sys)} {sum(f.startswith('ungrounded:eligibility.') for f in flags)}")
    if rep:
        out += ["", f"Gates reset to unknown across all repeats: {'; '.join(rep)}."]
    return out + [""]


def _costs(r: dict) -> list[str]:
    s = r["scores"]
    out = ["## Cost, tokens and latency (API calls made by this run)", ""]
    if s["calls"]:
        out += [("| Purpose, prompt | calls (ok) | tokens in | tokens out | cost USD | latency "
                 "median / mean / max |"), "|---|---|---|---|---|---|"]
        for key, c in s["calls"].items():
            lat = c["latency_ms"]
            est = f", {c['estimated']} estimated" if c["estimated"] else ""
            out.append(f"| {key} | {c['calls']} ({c['ok']}{est}) | {c['input_tokens']:,} | "
                       f"{c['output_tokens']:,} | ${c['cost_usd']} | "
                       f"{lat['median'] / 1000:.1f} / {lat['mean'] / 1000:.1f} / "
                       f"{lat['max'] / 1000:.1f} s |")
        out.append("")
    else:
        out += ["No API calls.", ""]
    cached = ", ".join(
        f"{display(sys)} {sum(c['systems'][sys]['outcome'] == 'cache_hit' for c in r['cases'])}"
        f"/{s['n']}" for sys in llm_systems(r["systems"]))
    out += [(f"Primary runs already stored before this run (not counted here): {cached}. "
             "`extract`: a site extraction made now where missing. `eval`: eval-only primary "
             "runs and the repeats. The baseline makes no API call and costs nothing."), ""]
    return out


def _cases(r: dict) -> list[str]:
    systems = r["systems"]
    out = ["## Per-case disagreements", "",
           ("Every field where any system differs from the label, with the evidence quotes. "
            "⚠ = labeller marked the case unsure."), ""]
    for c in r["cases"]:
        rows = scoring.disagreements(c, systems)
        head = f"### {c['case']} · `{c['reference']}` · {c['notice_kind']}"
        if c["unsure"]:
            head += " ⚠ unsure"
        out += [head, ""]
        if c["unsure"]:
            out += [f"> Labeller's note: {c['notes']}", ""]
        failed = "".join(f" {display(s)} extraction failed: {c['systems'][s]['error']}."
                         for s in systems if not c["systems"][s]["ok"])
        out += [(f"Status: expected **{c['label']['status']}**, "
                 + ", ".join(f"{display(s)} **{c['systems'][s]['status']}**" for s in systems)
                 + "." + failed), ""]
        if not rows:
            out += ["All 8 fields match the label for every system.", ""]
            continue
        for row in rows:
            out.append(f"- **{row['field']}**: label {_code(row['label']['value'])} · "
                       + " · ".join(f"{display(s)} {_code(row[s]['value'])} ({row[s]['kind']})"
                                    for s in systems))
            if row["field"] == "criteria_location":
                continue
            gate = FIELDS[row["field"]][0]
            out.append(f"  - label: {_quote(row['label']['evidence'])}")
            for s in systems:
                note = ""
                if f"ungrounded:eligibility.{gate}" in c["systems"][s]["flags"]:
                    note = " (its evidence was not found in the notice, so the gate was reset " \
                           "to unknown)"
                out.append(f"  - {display(s)}: {_quote(row[s]['evidence'])}{note}")
        out.append("")
    return out


def render(r: dict) -> str:
    out = [f"# Eval report: {r['split']} split, {r['date']}", ""]
    for part in (_protocol, _summary, _error_types, _status, _changes, _consistency,
                 _grounding, _costs, _cases):
        out += part(r)
    return "\n".join(out).rstrip() + "\n"


def check_tag(tag: str) -> None:
    """A report tag is a file-name part: letters, digits, '.', '_' and inner '-'."""
    if tag and not re.fullmatch(r"[A-Za-z0-9_.]+(-[A-Za-z0-9_.]+)*", tag):
        raise ValueError(f"bad report tag {tag!r}")


def write(r: dict, reports_dir: Path, tag: str = "") -> tuple[Path, Path]:
    """eval/reports/<split>[-<tag>]-<date>.md and .json; a later run on the same day gets
    -2, -3..."""
    check_tag(tag)
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = f"{r['split']}-{tag}-{r['date']}" if tag else f"{r['split']}-{r['date']}"
    stem, i = base, 1
    while (reports_dir / f"{stem}.md").exists() or (reports_dir / f"{stem}.json").exists():
        i += 1
        stem = f"{base}-{i}"
    md, js = reports_dir / f"{stem}.md", reports_dir / f"{stem}.json"
    md.write_text(render(r), encoding="utf-8")
    js.write_text(json.dumps(r, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return md, js
