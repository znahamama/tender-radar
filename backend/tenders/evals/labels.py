"""Label files: render a notice into a YAML label template, and check a filled-in file.

A label file is a YAML comment block holding the notice (metadata and the full redacted
description) followed by the fields to fill. The checker reads the notice back from that
comment block, so a file is checked against exactly the text its labeller saw, with the same
normalization as the grounding check (tenders.extraction.validate).
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tenders.extraction.validate import is_grounded, normalize, normalize_loose

WIDTH = 100
TODO = "TODO"
RULE = "# " + "=" * (WIDTH - 2)
CONT = "#   "  # continuation lines (wrapped values and the description)

# Header fields in order. Every one except url is a grounding source, as in the prompt.
META_FIELDS = ("reference", "title", "buyer", "notice_type", "procurement_categories",
               "regions_of_delivery", "regions_of_opportunity", "trade_agreements",
               "closing_date", "url")
NOT_SOURCES = frozenset({"title", "url"})  # title is a source, but not a "metadata value"
REGION_FIELDS = frozenset({"regions_of_delivery", "regions_of_opportunity"})
DESCRIPTION_HEAD = "# description (redacted):"

YES_NO_UNKNOWN = ("yes", "no", "unknown")
CLEARANCE = ("none", "reliability", "secret", "top_secret", "unknown")
CRITERIA_LOCATION = ("stated_in_notice", "partly_stated", "see_solicitation")

# gate -> (value fields with allowed values, detail fields)
GATES: dict[str, tuple[dict[str, tuple[str, ...]], tuple[str, ...]]] = {
    "supply_arrangement_required": ({"value": YES_NO_UNKNOWN}, ("name",)),
    "supplier_origin_restriction": (
        {"value": ("none", "canadian_only", "canadian_and_trading_partners", "unknown")}, ()),
    "clearance": ({"corporate": CLEARANCE, "personnel": CLEARANCE}, ()),
    "work_location": ({"on_site": YES_NO_UNKNOWN}, ("place",)),
    "land_claims_applies": ({"value": YES_NO_UNKNOWN}, ()),
    "documents_behind_login": ({"value": YES_NO_UNKNOWN}, ("portal",)),
}

BODY = """\
criteria_location: TODO   # stated_in_notice | partly_stated | see_solicitation
gates:
  # value: yes | no | unknown. name: the arrangement as written in the notice, or null
  supply_arrangement_required: {value: TODO, name: null, evidence: null}
  # value: none | canadian_only | canadian_and_trading_partners | unknown
  supplier_origin_restriction: {value: TODO, evidence: null}
  # corporate, personnel: none | reliability | secret | top_secret | unknown
  clearance: {corporate: TODO, personnel: TODO, evidence: null}
  # on_site: yes | no | unknown. place: where staff must work (title/description), or null
  work_location: {on_site: TODO, place: null, evidence: null}
  # value: yes | no | unknown
  land_claims_applies: {value: TODO, evidence: null}
  # value: yes | no | unknown. portal: as written in the notice, or null
  documents_behind_login: {value: TODO, portal: null, evidence: null}
unsure: false   # true only for a genuinely ambiguous case, with a one-line note
notes: ""
"""
BODY_KEYS = ("criteria_location", "gates", "unsure", "notes")


def fmt(value) -> str:
    """Format a value the way the prompt renders metadata (tenders.extraction.prompt)."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(str(x) for x in value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _wrap(text: str, first: str) -> list[str]:
    lines = textwrap.wrap(text, width=WIDTH, initial_indent=first, subsequent_indent=CONT,
                          break_long_words=False, break_on_hyphens=False)
    return lines or [first.rstrip()]


def render(case_id: str, meta: dict[str, str], description: str) -> str:
    """The full label file for one notice: comment block, then the fields to fill."""
    banner = f"# Eval case {case_id}. Do not edit this comment block: check_labels reads the notice"
    out = [RULE, banner, "# text from it. Guide: docs/label-guide.md", RULE]
    for key in META_FIELDS:
        out += _wrap(meta.get(key, "").strip(), f"# {key}: ")
    out.append(DESCRIPTION_HEAD)
    for line in description.splitlines():
        out += _wrap(line.strip(), CONT) if line.strip() else ["#"]
    out += [RULE, "", BODY]
    return "\n".join(out)


@dataclass
class Header:
    meta: dict[str, str] = field(default_factory=dict)
    description: str = ""

    def sources(self) -> Sources:
        """The notice as the grounding check sees it: title, metadata values, description."""
        meta = {k: v for k, v in self.meta.items() if k not in NOT_SOURCES and v}
        texts = [t for t in (self.meta.get("title", ""), *meta.values(), self.description) if t]
        no_regions = [t for t in (self.meta.get("title", ""), self.description,
                                  *(v for k, v in meta.items() if k not in REGION_FIELDS)) if t]
        return Sources(
            quotes=[normalize(t) for t in texts],
            whole_values=frozenset(normalize(form) for k, v in meta.items()
                                   for form in (v, f"{k}: {v}")),
            details=[normalize_loose(t) for t in texts],
            places=[normalize_loose(t) for t in no_regions],
        )


@dataclass(frozen=True)
class Sources:
    quotes: list[str]  # normalize()d, for evidence
    whole_values: frozenset[str]  # a whole metadata value is valid evidence at any length
    details: list[str]  # normalize_loose()d, for SA name and portal
    places: list[str]  # same, without the regions values (a delivery region is no work site)


_FIELD = re.compile(r"^# ([a-z_]+):(?: (.*))?$")


def parse_header(text: str) -> Header:
    """Read the notice back from the comment block render() wrote."""
    h, parts, key, in_desc = Header(), {}, None, False
    lines = text.splitlines()
    try:
        start = lines.index(RULE, 1) + 1  # after the opening banner
    except ValueError:
        return h
    for line in lines[start:]:
        if line == RULE:
            break
        if in_desc:
            if line.startswith(CONT):
                parts["description"].append(line[len(CONT):])
            continue
        if line == DESCRIPTION_HEAD:
            in_desc, key = True, "description"
            parts[key] = []
            continue
        m = _FIELD.match(line)
        if m:
            key = m.group(1)
            parts[key] = [m.group(2) or ""]
        elif key and line.startswith(CONT):
            parts[key].append(line[len(CONT):])
    for k, chunks in parts.items():
        value = " ".join(c for c in chunks if c)
        if k == "description":
            h.description = value
        else:
            h.meta[k] = value
    return h


@dataclass
class FileReport:
    path: Path
    reference: str = ""
    todo: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.todo == 0 and not self.problems


def _count_todo(node) -> int:
    if isinstance(node, dict):
        return sum(_count_todo(v) for v in node.values())
    if isinstance(node, list):
        return sum(_count_todo(v) for v in node)
    return int(isinstance(node, str) and node.strip() == TODO)


def _value(raw, allowed: tuple[str, ...]):
    """YAML 1.1 reads bare yes/no as booleans; map them back for yes/no/unknown fields."""
    if isinstance(raw, bool) and "yes" in allowed:
        return "yes" if raw else "no"
    return raw


def check_text(path: Path, text: str) -> FileReport:
    report = FileReport(path)
    header = parse_header(text)
    report.reference = header.meta.get("reference", "")
    if not report.reference or not header.description:
        report.problems.append("notice comment block missing or damaged")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        report.problems.append(f"YAML does not parse{where}: {getattr(e, 'problem', e)}")
        return report
    if not isinstance(data, dict):
        report.problems.append("no label fields found")
        return report

    report.todo = _count_todo(data)
    p = report.problems
    keys = set(data)
    if missing := [k for k in BODY_KEYS if k not in keys]:
        p.append(f"missing field(s): {', '.join(missing)}")
    if extra := sorted(keys - set(BODY_KEYS)):
        p.append(f"unexpected field(s): {', '.join(map(str, extra))}")

    crit = data.get("criteria_location")
    if crit not in (TODO, None) and crit not in CRITERIA_LOCATION:
        p.append(f"criteria_location: {crit!r} is not one of {', '.join(CRITERIA_LOCATION)}")

    unsure, notes = data.get("unsure"), data.get("notes")
    if not isinstance(unsure, bool):
        p.append("unsure: must be true or false")
    if notes is None:
        notes = ""
    if not isinstance(notes, str):
        p.append("notes: must be a string in quotes")
    elif unsure is True and not notes.strip():
        p.append("unsure: true needs a one-line note in notes")
    elif "\n" in notes.strip():
        p.append("notes: keep it to one line")

    gates = data.get("gates")
    if not isinstance(gates, dict):
        p.append("gates: missing or not a mapping")
        return report
    if extra := sorted(set(gates) - set(GATES)):
        p.append(f"gates: unexpected gate(s): {', '.join(map(str, extra))}")
    src = header.sources()
    for gate, (value_fields, detail_fields) in GATES.items():
        _check_gate(gate, gates.get(gate), value_fields, detail_fields, src, p)
    return report


def _check_gate(gate: str, g, value_fields: dict[str, tuple[str, ...]],
                detail_fields: tuple[str, ...], src: Sources, p: list[str]) -> None:
    if not isinstance(g, dict):
        p.append(f"{gate}: missing or not a mapping")
        return
    expected = {*value_fields, *detail_fields, "evidence"}
    if missing := sorted(expected - set(g)):
        p.append(f"{gate}: missing {', '.join(missing)}")
    if extra := sorted(set(map(str, g)) - expected):
        p.append(f"{gate}: unexpected {', '.join(extra)}")

    values = {}
    for f, allowed in value_fields.items():
        v = _value(g.get(f), allowed)
        values[f] = v
        if v == TODO:
            continue
        if v not in allowed:
            hint = " (write none, not no)" if v is False and "none" in allowed else ""
            p.append(f"{gate}.{f}: {v!r} is not one of {' | '.join(allowed)}{hint}")

    filled = [v for v in values.values() if v != TODO]
    stated = any(values[f] in allowed and values[f] != "unknown"
                 for f, allowed in value_fields.items())
    evidence = g.get("evidence")
    if evidence is not None and not isinstance(evidence, str):
        p.append(f"{gate}.evidence: must be a quoted string or null")
        evidence = None
    if stated:
        if evidence is None:
            p.append(f"{gate}.evidence: a value other than unknown needs evidence")
        elif not is_grounded(evidence, src.quotes, src.whole_values):
            p.append(f"{gate}.evidence: not an exact copy of the notice text (3+ words and "
                     "15+ characters, or a whole metadata value)")
    elif evidence is not None and filled and all(v == "unknown" for v in filled):
        p.append(f"{gate}.evidence: value is unknown, so evidence should be null "
                 "(use notes instead)")

    for f in detail_fields:
        d = g.get(f)
        if d is None:
            continue
        if not isinstance(d, str) or not d.strip():
            p.append(f"{gate}.{f}: must be a quoted string or null")
            continue
        main = values[next(iter(value_fields))]
        if main not in ("yes", TODO):
            p.append(f"{gate}.{f}: only filled when the value is yes")
        allowed = src.places if f == "place" else src.details
        nd = normalize_loose(d)
        if not nd or not any(nd in s for s in allowed):
            where = " (not counting the regions values)" if f == "place" else ""
            p.append(f"{gate}.{f}: {d!r} does not appear in the notice{where}")


def check_file(path: Path) -> FileReport:
    return check_text(path, path.read_text(encoding="utf-8"))
