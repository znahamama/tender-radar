# Reuse the extraction fixtures: pinned LLM settings, no real API client, tender factory.
from tenders.tests.extraction.conftest import (  # noqa: F401
    make_tender,
    meter,
    no_real_api,
    pin_llm_settings,
    valid_data,
)

# Shaped like cb-8448-42897985 (a request to qualify for ProServices); text invented.
SA_REQUEST_NOTICE = {
    "reference_number": "cb-0000-00000001",
    "title": "ProServices Method of Supply",
    "description": ("Canada is inviting suppliers to qualify for the ProServices supply "
                    "arrangement. Locations in areas subject to Comprehensive Land Claims "
                    "Agreements are excluded from this solicitation."),
    "notice_type": "Request for Supply Arrangement",
    "notice_kind": "sa_or_so_request",
    "sa_required_from_metadata": None,
    "region_scope": "national",
}


def sa_request_output():
    d = valid_data(criteria_location="see_solicitation", deliverables=[], deadlines=[],
                   mandatory_requirements=[])
    e = d["eligibility"]
    e["supply_arrangement_required"] = {"value": "yes", "name": "ProServices",
                                        "evidence": "Request for Supply Arrangement"}
    e["clearance"] = {"corporate": "unknown", "personnel": "unknown", "evidence": None}
    e["work_location"] = {"place": None, "on_site": "unknown", "evidence": None}
    e["documents_behind_login"] = {"value": "unknown", "portal": None, "evidence": None}
    e["land_claims_applies"] = {
        "value": "yes", "evidence": "areas subject to Comprehensive Land Claims Agreements"}
    return d
