"""Pure scoring for the eval harness: labels vs system outputs. No database, no network.

A "case" here is a plain dict built by tenders.evals.harness:

    {"case": "dev-01", "reference": ..., "unsure": bool, "notes": str,
     "label": {"fields": {...}, "evidence": {...}, "status": ...},
     "systems": {"llm_v2": {"fields": {...}, "evidence": {...}, "status": ..., ...},
                 "llm_v3": {...}, "baseline": {...}},
     "repeats": {"llm_v2": [{"fields": {...}, "status": ..., ...}, ...], "llm_v3": [...]}}

``fields`` maps each scored field to its value; a system value of "unknown" (or None, for a
failed extraction) means the system stated nothing. Which systems a run has is listed in the
run's "systems" (LLM systems are named ``llm_<prompt version>``); functions take that list.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

from tenders.eligibility.rules import Profile, TenderFacts, evaluate

from .labels import GATES as LABEL_GATES
from .labels import _value

UNKNOWN = "unknown"
FAILED = "(failed)"  # a repeat whose extraction failed, in the consistency tables
BASELINE = "baseline"
STATUSES = ("blocked", "check_solicitation", "no_blockers_stated")
ERROR_TYPES = ("correct", "wrong", "missed", "overclaimed")

# field -> (gate, key inside the gate); criteria_location is top level.
FIELDS: dict[str, tuple[str | None, str]] = {
    "criteria_location": (None, "criteria_location"),
    "supply_arrangement_required": ("supply_arrangement_required", "value"),
    "supplier_origin_restriction": ("supplier_origin_restriction", "value"),
    "clearance.corporate": ("clearance", "corporate"),
    "clearance.personnel": ("clearance", "personnel"),
    "work_location.on_site": ("work_location", "on_site"),
    "land_claims_applies": ("land_claims_applies", "value"),
    "documents_behind_login": ("documents_behind_login", "value"),
}


# --- reading values -------------------------------------------------------------------

def system_fields(data: dict | None) -> dict[str, str | None]:
    """Scored values from post-validated extraction data (None: the extraction failed)."""
    out: dict[str, str | None] = {}
    for name, (gate, key) in FIELDS.items():
        if data is None:
            out[name] = None
        elif gate is None:
            out[name] = data.get(key)
        else:
            out[name] = data["eligibility"][gate][key]
    return out


def evidence_by_field(gates: dict | None) -> dict[str, str | None]:
    """The gate's evidence quote for each scored field (None for criteria_location)."""
    return {name: (gates or {}).get(gate, {}).get("evidence") if gate else None
            for name, (gate, _) in FIELDS.items()}


@dataclass(frozen=True)
class Label:
    criteria_location: str
    gates: dict  # gate -> dict shaped like ExtractionData.eligibility.<gate>
    unsure: bool
    notes: str

    @property
    def fields(self) -> dict[str, str]:
        return system_fields(self.as_data())

    def as_data(self) -> dict:
        """The labels shaped like post-validated extraction data, for the eligibility rules."""
        return {"criteria_location": self.criteria_location, "eligibility": self.gates}


def parse_label(text: str) -> Label:
    """A label file checked by check_labels. Bare yes/no (YAML booleans) map back to strings."""
    raw = yaml.safe_load(text)
    gates = {}
    for gate, (value_fields, detail_fields) in LABEL_GATES.items():
        g = raw["gates"][gate]
        gates[gate] = {
            **{f: _value(g[f], allowed) for f, allowed in value_fields.items()},
            **{f: g.get(f) for f in detail_fields},
            "evidence": g.get("evidence"),
        }
    return Label(raw["criteria_location"], gates, bool(raw.get("unsure")),
                 (raw.get("notes") or "").strip())


def load_label(path: Path) -> Label:
    return parse_label(path.read_text(encoding="utf-8"))


# --- scoring ---------------------------------------------------------------------------

def classify(label: str, system: str | None) -> str:
    """correct / wrong / missed / overclaimed. "unknown" == "unknown" is correct."""
    system = UNKNOWN if system is None else system
    if system == label:
        return "correct"
    if system == UNKNOWN:
        return "missed"
    if label == UNKNOWN:
        return "overclaimed"
    return "wrong"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for k successes out of n (None when n is 0)."""
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def tally(cases: list[dict], system: str, field: str) -> dict:
    """Error-type counts and accuracy for one system and field over ``cases``."""
    counts = Counter({t: 0 for t in ERROR_TYPES})
    unknown = 0
    for c in cases:
        value = c["systems"][system]["fields"][field]
        counts[classify(c["label"]["fields"][field], value)] += 1
        unknown += value in (None, UNKNOWN)
    n = len(cases)
    ci = wilson(counts["correct"], n)
    return {**counts, "n": n, "system_unknown": unknown,
            "accuracy": counts["correct"] / n if n else None,
            "ci95": list(ci) if ci else None}


def field_scores(cases: list[dict], systems: list[str]) -> dict:
    """{system: {field: tally}} for every system and scored field."""
    return {s: {f: tally(cases, s, f) for f in FIELDS} for s in systems}


def expected_status(label: Label, facts: TenderFacts, profile: Profile) -> str:
    """The status the eligibility rules give the labelled gates (no extraction flags)."""
    return evaluate(label.as_data(), [], facts, profile).status


def confusion(cases: list[dict], system: str) -> dict:
    """{expected: {got: count}} over STATUSES, plus false and missed blocks."""
    table = {e: {g: 0 for g in STATUSES} for e in STATUSES}
    false_blocks, missed_blocks = [], []
    for c in cases:
        expected, got = c["label"]["status"], c["systems"][system]["status"]
        table[expected][got] += 1
        if got == "blocked" and expected != "blocked":
            false_blocks.append(c["case"])
        if expected == "blocked" and got != "blocked":
            missed_blocks.append(c["case"])
    agree = sum(table[s][s] for s in STATUSES)
    ci = wilson(agree, len(cases))
    return {"table": table, "agree": agree, "n": len(cases), "ci95": list(ci) if ci else None,
            "false_blocks": false_blocks, "missed_blocks": missed_blocks}


def consistency(cases: list[dict], system: str) -> dict:
    """Run-to-run agreement of one LLM system: its primary run plus every repeat.

    Per field (and for the status): how many cases gave the same answer in every run, and
    the cases that flip, with the answer of each run. A failed run counts as its own answer.
    Cases with no repeats are left out.
    """
    reps = {c["case"]: c.get("repeats", {}).get(system, []) for c in cases}
    with_runs = [c for c in cases if reps[c["case"]]]
    out: dict = {"cases": len(with_runs),
                 "runs_per_case": {c["case"]: 1 + len(reps[c["case"]]) for c in with_runs},
                 "fields": {}}
    for name in (*FIELDS, "status"):
        stable, flips = 0, []
        for c in with_runs:
            runs = [c["systems"][system], *reps[c["case"]]]
            values = [_run_value(r, name) for r in runs]
            if len(set(values)) == 1:
                stable += 1
            else:
                flips.append({"case": c["case"], "label": _label_value(c, name),
                              "values": values})
        out["fields"][name] = {"stable": stable, "n": len(with_runs), "flips": flips}
    return out


def _run_value(run: dict, name: str) -> str:
    if name == "status":
        return run["status"]
    if not run.get("ok", True):
        return FAILED
    value = run["fields"][name]
    return UNKNOWN if value is None else value


def _label_value(case: dict, name: str) -> str:
    return case["label"]["status"] if name == "status" else case["label"]["fields"][name]


def disagreements(case: dict, systems: list[str]) -> list[dict]:
    """Fields where any system differs from the label, with every evidence quote."""
    rows = []
    for name in FIELDS:
        label = case["label"]["fields"][name]
        got = {s: case["systems"][s]["fields"][name] for s in systems}
        kinds = {s: classify(label, got[s]) for s in systems}
        if all(k == "correct" for k in kinds.values()):
            continue
        rows.append({
            "field": name,
            "label": {"value": label, "evidence": case["label"]["evidence"][name]},
            **{s: {"value": UNKNOWN if got[s] is None else got[s], "kind": kinds[s],
                   "evidence": case["systems"][s]["evidence"][name]} for s in systems},
        })
    return rows


def changes(cases: list[dict], before: str, after: str) -> dict:
    """Primary-run answers that differ between two systems (e.g. prompt v2 -> v3), per field.

    ``fixed``: not correct before, correct after. ``broken``: the reverse. ``other``: a
    different answer that is still not correct. ``fields`` counts the gates and
    criteria_location only; status rows are listed but not counted."""
    rows = []
    for c in cases:
        label = c["label"]["fields"]
        for name in (*FIELDS, "status"):
            if name == "status":
                lab = c["label"]["status"]
                a, b = c["systems"][before]["status"], c["systems"][after]["status"]
                ka, kb = ("correct" if a == lab else "wrong"), ("correct" if b == lab else "wrong")
            else:
                lab = label[name]
                a, b = (c["systems"][s]["fields"][name] for s in (before, after))
                a, b = (UNKNOWN if v is None else v for v in (a, b))
                ka, kb = classify(lab, a), classify(lab, b)
            if a == b:
                continue
            change = ("fixed" if kb == "correct" else
                      "broken" if ka == "correct" else "other")
            rows.append({"case": c["case"], "field": name, "label": lab, "unsure": c["unsure"],
                         "before": {"value": a, "kind": ka}, "after": {"value": b, "kind": kb},
                         "change": change})
    counts = Counter(r["change"] for r in rows if r["field"] != "status")
    return {"before": before, "after": after, "rows": rows,
            "fields": {k: counts.get(k, 0) for k in ("fixed", "broken", "other")}}
