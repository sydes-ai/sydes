"""Regression test for a real attribution bug found on a live PR
(`sydes-examples/demo-orders-api#5`, "Reject orders with insufficient
stock"): a validation branch that rejects via `except <DomainError>: raise
HTTPException(...)` -- rather than an inline `if <condition>: raise ...` in
the same statement -- was classified as a generic "transform" step, not
"validation_branch". `_trace_obligations` only turns "validation_branch"
steps into obligations, so no obligation at all was ever derived for the new
behavior; the new regression test for it landed, by pattern only, on
unrelated pre-existing generic-validation obligations ("rejects invalid
payloads", etc.), while the report treated a happy-path test as "the"
established evidence for the change instead.

Fix: `trace/function_body_slicer.py` now also recognizes a bare `raise`/
`throw` that names its own 4xx status (no `if` required) as a rejection
branch. `_rejection_status_after` (below) also had to start checking the
branch step's own line for a status literal, not just the following ones --
once the branch itself can carry an inline status, peeking only ahead could
walk past it into an unrelated sibling `except` clause's status instead.
"""

from __future__ import annotations

from sydes.core.models import ApiResponseContract, ApiRouteContract
from sydes.verify.models import AffectedFlow, ChangedSymbol, Hunk
from sydes.verify.obligations import derive_obligations


def _symbol(file: str, start_line: int, end_line: int) -> ChangedSymbol:
    return ChangedSymbol(
        id=f"{file}:{start_line}",
        repo="api",
        file=file,
        name="create_order",
        start_line=start_line,
        end_line=end_line,
    )


def test_except_raise_http_exception_becomes_a_validation_obligation() -> None:
    """The exact shape from the live PR: a Python `except` clause translating
    a caught domain error into `raise HTTPException(status_code=400, ...)`,
    with no `if` anywhere in that statement."""
    steps = [
        {
            "kind": "handler",
            "name": "handler",
            "detail": "create_order",
            "file": "app/main.py",
            "line_start": 30,
        },
        {
            "kind": "transform",
            "name": "transform",
            "detail": "except InsufficientStockError as exc:",
            "file": "app/main.py",
            "line_start": 35,
        },
        {
            "kind": "validation_branch",
            "name": "validation_branch",
            "detail": 'raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient stock") from exc',
            "file": "app/main.py",
            "line_start": 36,
        },
    ]
    flow = AffectedFlow(
        id="flow:POST:/orders",
        entry_label="POST /orders",
        steps=steps,
        artifact_refs={"handler_file": "app/main.py"},
    )
    # The real diff touched both files: the new except-clause in the handler
    # (app/main.py) and the new business-rule check in the service it calls
    # (app/service.py) -- `introduced_by_change` for a validation obligation
    # comes from the handler itself being changed, not from the specific
    # step's own line falling inside a changed span (see `handler_changed`
    # in `derive_obligations`).
    changed_symbols = [
        _symbol("app/main.py", 30, 37),
        _symbol("app/service.py", 20, 24),
    ]

    obligations = derive_obligations(
        flow=flow,
        route_contract=None,
        test_matrix=None,
        changed_symbols=changed_symbols,
        changed_files={"app/main.py", "app/service.py"},
    )

    rejection = [o for o in obligations if "insufficient stock" in o.statement.lower()]
    assert len(rejection) == 1, obligations
    ob = rejection[0]
    assert ob.kind == "validation"
    assert ob.introduced_by_change is True
    assert ob.statement.endswith("and responds 400")


def test_rejection_status_prefers_the_branchs_own_line_over_a_sibling_except() -> None:
    """Two sibling `except` clauses, each with its own inline status. Before
    the fix, the first branch's status lookup peeked only at *following*
    steps and could walk into the second clause's status instead of its own."""
    steps = [
        {
            "kind": "validation_branch",
            "detail": 'raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SKU not found") from exc',
            "file": "app/main.py",
            "line_start": 34,
        },
        {
            "kind": "transform",
            "detail": "except InsufficientStockError as exc:",
            "file": "app/main.py",
            "line_start": 35,
        },
        {
            "kind": "validation_branch",
            "detail": 'raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient stock") from exc',
            "file": "app/main.py",
            "line_start": 36,
        },
    ]
    flow = AffectedFlow(id="flow:POST:/orders", entry_label="POST /orders", steps=steps)
    changed_symbols = [_symbol("app/main.py", 30, 40)]

    obligations = derive_obligations(
        flow=flow,
        route_contract=None,
        test_matrix=None,
        changed_symbols=changed_symbols,
        changed_files={"app/main.py"},
    )

    by_status = {}
    for ob in obligations:
        if "sku not found" in ob.statement.lower():
            by_status["404"] = ob
        elif "insufficient stock" in ob.statement.lower():
            by_status["400"] = ob

    assert by_status["404"].statement.endswith("and responds 404")
    assert by_status["400"].statement.endswith("and responds 400")


def test_typescript_throw_with_inline_status_becomes_a_validation_obligation() -> None:
    """The same idiom in a brace language: no `if`, a `catch` block that
    throws an HttpException naming its own status code."""
    steps = [
        {
            "kind": "transform",
            "detail": "} catch (err) {",
            "file": "src/orders.controller.ts",
            "line_start": 12,
        },
        {
            "kind": "validation_branch",
            "detail": "throw new HttpException('Insufficient stock', 400);",
            "file": "src/orders.controller.ts",
            "line_start": 13,
        },
    ]
    flow = AffectedFlow(
        id="flow:POST:/orders",
        entry_label="POST /orders",
        steps=steps,
        artifact_refs={"handler_file": "src/orders.controller.ts"},
    )
    changed_symbols = [_symbol("src/orders.controller.ts", 5, 15)]

    obligations = derive_obligations(
        flow=flow,
        route_contract=None,
        test_matrix=None,
        changed_symbols=changed_symbols,
        changed_files={"src/orders.controller.ts"},
    )

    rejection = [o for o in obligations if "insufficient stock" in o.statement.lower()]
    assert len(rejection) == 1, obligations
    assert rejection[0].introduced_by_change is True
    assert rejection[0].statement.endswith("and responds 400")


# ---------------------------------------------------------------------------
# Precise diff-hunk attribution (`changed_file_hunks`). The three tests below
# reproduce the exact real-world correction found by re-running PR #5's real
# diff through `resolve_change_set`: `app/main.py` hunks were `[(4, 9), (35,
# 36)]`. The new 400 rejection's own line (36) falls inside a hunk; the
# pre-existing 404 rejection's line (34) does not, even though both sit
# inside the SAME changed `create_order` handler (lines 30-37) -- exactly
# the file-vs-line distinction `handler_changed` alone cannot make. The 201
# route-contract skeleton has no attributable line of its own at all.
# ---------------------------------------------------------------------------

_PR5_MAIN_PY_HUNKS = {"app/main.py": [Hunk(start_line=4, end_line=9), Hunk(start_line=35, end_line=36)]}


def _pr5_flow_steps() -> list[dict]:
    return [
        {
            "kind": "handler",
            "name": "handler",
            "detail": "create_order",
            "file": "app/main.py",
            "line_start": 30,
        },
        {
            "kind": "transform",
            "detail": "except UnknownSkuError as exc:",
            "file": "app/main.py",
            "line_start": 33,
        },
        {
            "kind": "validation_branch",
            "detail": 'raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SKU not found") from exc',
            "file": "app/main.py",
            "line_start": 34,
        },
        {
            "kind": "transform",
            "detail": "except InsufficientStockError as exc:",
            "file": "app/main.py",
            "line_start": 35,
        },
        {
            "kind": "validation_branch",
            "detail": 'raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient stock") from exc',
            "file": "app/main.py",
            "line_start": 36,
        },
    ]


def test_new_rejection_line_inside_changed_hunk_is_introduced_by_change() -> None:
    """PR #5 case 1: the new 400 rejection's own line (36) falls inside the
    diff's `(35, 36)` hunk for `app/main.py`."""
    flow = AffectedFlow(
        id="flow:POST:/orders",
        entry_label="POST /orders",
        steps=_pr5_flow_steps(),
        artifact_refs={"handler_file": "app/main.py"},
    )
    changed_symbols = [_symbol("app/main.py", 30, 37), _symbol("app/service.py", 20, 24)]

    obligations = derive_obligations(
        flow=flow,
        route_contract=None,
        test_matrix=None,
        changed_symbols=changed_symbols,
        changed_files={"app/main.py", "app/service.py"},
        changed_file_hunks=_PR5_MAIN_PY_HUNKS,
    )

    rejection = [o for o in obligations if "insufficient stock" in o.statement.lower()]
    assert len(rejection) == 1, obligations
    assert rejection[0].introduced_by_change is True


def test_preexisting_sibling_branch_outside_changed_hunk_is_not_introduced() -> None:
    """PR #5 case 2: the pre-existing 404 rejection's own line (34) is
    *outside* both hunks, even though it lives in the same changed handler
    (lines 30-37) as the new 400 rejection. This is exactly what
    `handler_changed` alone got wrong -- it promoted both branches
    identically because it only knows the file/symbol changed, not which
    lines within it did."""
    flow = AffectedFlow(
        id="flow:POST:/orders",
        entry_label="POST /orders",
        steps=_pr5_flow_steps(),
        artifact_refs={"handler_file": "app/main.py"},
    )
    changed_symbols = [_symbol("app/main.py", 30, 37), _symbol("app/service.py", 20, 24)]

    obligations = derive_obligations(
        flow=flow,
        route_contract=None,
        test_matrix=None,
        changed_symbols=changed_symbols,
        changed_files={"app/main.py", "app/service.py"},
        changed_file_hunks=_PR5_MAIN_PY_HUNKS,
    )

    sku_not_found = [o for o in obligations if "sku not found" in o.statement.lower()]
    assert len(sku_not_found) == 1, obligations
    assert sku_not_found[0].introduced_by_change is False


def test_synthesized_route_contract_with_no_source_line_is_not_introduced() -> None:
    """PR #5 case 3: the 201 success contract is a synthesized, route-level
    obligation with no attributable source line of its own (it comes from
    the declared response contract, not a trace step). With real hunk data
    available for the handler's file, it must not default to "introduced"
    just because something else in that file changed."""
    contract = ApiRouteContract(
        method="POST",
        path="/orders",
        handler="create_order",
        file="app/main.py",
        responses={"201": ApiResponseContract(status=201, description="Default 201 response skeleton.")},
    )
    flow = AffectedFlow(
        id="flow:POST:/orders",
        entry_label="POST /orders",
        steps=_pr5_flow_steps(),
        artifact_refs={"handler_file": "app/main.py"},
    )
    changed_symbols = [_symbol("app/main.py", 30, 37), _symbol("app/service.py", 20, 24)]

    obligations = derive_obligations(
        flow=flow,
        route_contract=contract,
        test_matrix=None,
        changed_symbols=changed_symbols,
        changed_files={"app/main.py", "app/service.py"},
        changed_file_hunks=_PR5_MAIN_PY_HUNKS,
    )

    success = [o for o in obligations if "responds 201" in o.statement.lower()]
    assert len(success) == 1, obligations
    assert success[0].introduced_by_change is False


def test_generic_go_case_hunk_precision_without_framework_special_casing() -> None:
    """Non-FastAPI, non-HTTP-status-shaped generic case: a Go handler with a
    pre-existing validation branch and a newly-added one, to confirm the
    fix is about line/hunk overlap generically, not anything tied to a
    specific framework or status-code pattern."""
    steps = [
        {
            "kind": "validation_branch",
            "detail": "if !isAuthorized(user) { return ErrForbidden }",
            "file": "internal/orders/handler.go",
            "line_start": 10,
        },
        {
            "kind": "validation_branch",
            "detail": "if qty > stock { return ErrInsufficientStock }",
            "file": "internal/orders/handler.go",
            "line_start": 25,
        },
    ]
    flow = AffectedFlow(
        id="flow:POST:/orders",
        entry_label="POST /orders",
        steps=steps,
        artifact_refs={"handler_file": "internal/orders/handler.go"},
    )
    changed_symbols = [_symbol("internal/orders/handler.go", 1, 30)]
    hunks = {"internal/orders/handler.go": [Hunk(start_line=24, end_line=26)]}

    obligations = derive_obligations(
        flow=flow,
        route_contract=None,
        test_matrix=None,
        changed_symbols=changed_symbols,
        changed_files={"internal/orders/handler.go"},
        changed_file_hunks=hunks,
    )

    by_detail = {o.statement.split("`")[1]: o for o in obligations if "`" in o.statement}
    assert by_detail["if !isAuthorized(user) { return ErrForbidden }"].introduced_by_change is False
    assert by_detail["if qty > stock { return ErrInsufficientStock }"].introduced_by_change is True


# ---------------------------------------------------------------------------
# Regression: a real bug only surfaced by testing on the actual PR with real
# CBM-backed cross-function tracing (sydes-examples/demo-orders-api#5). A
# followed call into the callee produced the comparator condition
# (`if order.quantity > available_stock:`) as its own step, separate from
# the exception-translation raise in the caller. Both were classified
# `validation_branch`, so `_trace_obligations` created TWO obligations for
# what is really one branch: the condition (no status of its own, and the
# real status-bearing raise is in a different function this loop can't
# resolve locally) and the raise (the real one, with `responds 400`). A
# status-code-less obligation falls back to matching ANY 4xx-asserting test
# as evidence (`test_mapping.map_tests_to_obligation`'s "no codes" path),
# which wrongly attached `test_non_positive_quantity_is_rejected` (a 422, for
# an unrelated pre-existing validation rule) to the stock-check obligation.
# ---------------------------------------------------------------------------


def test_bare_condition_with_no_resolvable_status_defers_to_the_real_raise() -> None:
    """Cross-function case: the condition and its raise are in the callee,
    with no status anywhere near either -- the actual status only exists in
    a completely separate part of the flow (the caller's exception
    translation). The bare condition must not become its own obligation."""
    steps = [
        {
            "kind": "transform", "file": "app/main.py", "line_start": 35,
            "detail": "except InsufficientStockError as exc:",
        },
        {
            "kind": "validation_branch", "file": "app/main.py", "line_start": 36,
            "detail": 'raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient stock") from exc',
        },
        {"kind": "service_call", "file": "app/service.py", "line_start": None, "detail": "create_order"},
        {
            "kind": "transform", "file": "app/service.py", "line_start": 21,
            "detail": "available_stock = get_stock(order.sku)",
        },
        {
            "kind": "validation_branch", "file": "app/service.py", "line_start": 22,
            "detail": "if order.quantity > available_stock:",
        },
        {
            "kind": "transform", "file": "app/service.py", "line_start": 23,
            "detail": "raise InsufficientStockError(order.sku)",
        },
        {"kind": "response", "file": "app/service.py", "line_start": 25, "detail": "return repository.save_order(order)"},
    ]
    flow = AffectedFlow(
        id="flow:POST:/orders", entry_label="POST /orders", steps=steps,
        artifact_refs={"handler_file": "app/main.py"},
    )
    changed_symbols = [_symbol("app/main.py", 30, 37), _symbol("app/service.py", 20, 25)]
    hunks = {
        "app/main.py": [Hunk(start_line=35, end_line=36)],
        "app/service.py": [Hunk(start_line=21, end_line=24)],
    }

    obligations = derive_obligations(
        flow=flow, route_contract=None, test_matrix=None,
        changed_symbols=changed_symbols, changed_files={"app/main.py", "app/service.py"},
        changed_file_hunks=hunks,
    )

    validation_obligations = [o for o in obligations if o.kind == "validation"]
    assert len(validation_obligations) == 1, validation_obligations
    assert validation_obligations[0].statement.endswith("and responds 400")


def test_bare_condition_with_a_nearby_status_keeps_stable_wording_not_the_raises_message() -> None:
    """Same-function case: the condition and its raise ARE adjacent, and the
    raise DOES carry its own status -- exactly one obligation should result,
    built from the condition's stable wording (not the raise's message
    text), so that changing only the error message doesn't change this
    obligation's identity/statement."""
    steps = [
        {
            "kind": "validation_branch", "file": "routers/students.py", "line_start": 8,
            "detail": 'if not payload.get("name", "").strip():',
        },
        {
            "kind": "validation_branch", "file": "routers/students.py", "line_start": 9,
            "detail": 'raise HttpError(400, "Student name cannot be blank")',
        },
    ]
    flow = AffectedFlow(
        id="flow:POST:/students", entry_label="POST /students", steps=steps,
        artifact_refs={"handler_file": "routers/students.py"},
    )
    changed_symbols = [_symbol("routers/students.py", 6, 10)]
    hunks = {"routers/students.py": [Hunk(start_line=8, end_line=9)]}

    obligations = derive_obligations(
        flow=flow, route_contract=None, test_matrix=None,
        changed_symbols=changed_symbols, changed_files={"routers/students.py"},
        changed_file_hunks=hunks,
    )

    validation_obligations = [o for o in obligations if o.kind == "validation"]
    assert len(validation_obligations) == 1, validation_obligations
    ob = validation_obligations[0]
    assert "Student name cannot be blank" not in ob.statement
    assert "payload" in ob.statement
    assert ob.statement.endswith("and responds 400")
