"""Lightweight consistency checks across final contract and test-matrix artifacts."""

from __future__ import annotations

from typing import Any

from sydes.core.models import ApiContractArtifact, TestMatrix


def validate_artifact_consistency(
    api_contract: ApiContractArtifact | None,
    test_matrix: TestMatrix | None,
) -> list[str]:
    """Return non-fatal warnings when final artifacts disagree in obvious ways."""

    warnings: list[str] = []
    if api_contract is None or test_matrix is None:
        return warnings

    response_statuses = {
        str(status)
        for route in api_contract.routes
        for status in route.responses.keys()
    }
    post_has_201_only = any(
        (route.method or "").upper() == "POST"
        and "201" in route.responses
        and "200" not in route.responses
        for route in api_contract.routes
    )
    has_400 = any("400" in route.responses for route in api_contract.routes)

    categories: list[str] = []
    names: list[str] = []
    stronger_positive_exists = False
    field_specific_validation_exists = False
    tests: list[tuple[str, str | None, dict[str, Any], list[str]]] = []

    for group in test_matrix.groups:
        categories.append(group.category)
        for test in group.tests:
            names.append(test.name)
            expected = test.expected if isinstance(test.expected, dict) else {}
            status = expected.get("status")
            response_ref = expected.get("response_schema_ref")
            if response_ref is not None:
                ref_status = str(response_ref).split(".", 1)[-1]
                if ref_status not in response_statuses:
                    warnings.append(
                        f"scenario `{test.name}` references missing contract response `{response_ref}`"
                    )
            if status is not None and str(status) not in response_statuses:
                warnings.append(
                    f"scenario `{test.name}` expects status `{status}` absent from contract responses"
                )
            if (
                (test.category or "").lower() == "positive"
                and "responses.201" in test.contract_refs
                and expected.get("status") == 201
            ):
                stronger_positive_exists = True
            if (
                (test.category or "").lower() == "validation"
                and any(str(ref).startswith("request.body.") for ref in test.contract_refs)
            ):
                field_specific_validation_exists = True
            if (
                post_has_201_only
                and (test.category or "").lower() == "positive"
                and expected.get("status") == 200
            ):
                warnings.append(
                    f"positive POST scenario `{test.name}` still expects 200 while contract prefers 201"
                )
            if (
                has_400
                and (test.category or "").lower() == "validation"
                and expected.get("status") is None
            ):
                warnings.append(
                    f"validation scenario `{test.name}` is missing expected.status despite 400 contract response"
                )
            tests.append((test.name, test.category, expected, list(test.contract_refs)))

    if len(categories) != len(set(categories)):
        warnings.append("duplicate test-matrix categories remain after cleanup")
    if stronger_positive_exists:
        for name, category, expected, contract_refs in tests:
            if (category or "").lower() != "positive":
                continue
            if expected.get("status") == 201 and any(str(ref).startswith("responses.201") for ref in contract_refs):
                continue
            if "happy_path" in name or "creates_resource" in name or "returns_success" in name:
                warnings.append("generic happy-path scenario remains alongside stronger contract-aware positive scenario")
                break
    if field_specific_validation_exists and any(
        "rejects_invalid_payload" in name or "rejects_missing_required_field" in name
        for name in names
    ):
        warnings.append("generic validation scenario remains alongside stronger field-specific validation scenario")

    return warnings
