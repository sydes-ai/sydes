from sydes.core.models import EndpointCandidate
from sydes.trace.handler_resolver import (
    extract_handler_candidates,
    resolve_handler_reference,
)


def _endpoint(handler: str, file: str = "src/routes/router.ts") -> EndpointCandidate:
    return EndpointCandidate(
        method="POST",
        path="/items",
        handler=handler,
        file=file,
        repo="api",
    )


def test_extract_handler_candidates_unwraps_wrappers() -> None:
    parsed = extract_handler_candidates(
        "safeControllerFunction(asyncHandler(AttachmentController.createTaskAttachment))"
    )
    assert parsed["primary"]["normalized"] == "AttachmentController.createTaskAttachment"
    assert parsed["primary"]["wrappers"] == ["safeControllerFunction", "asyncHandler"]


def test_resolve_imported_class_method() -> None:
    index = {
        "files": [
            {
                "path": "src/routes/router.ts",
                "imports": [
                    {
                        "local": "AttachmentController",
                        "source": "../controllers/attachment-controller",
                        "resolved_file": "src/controllers/attachment-controller.ts",
                    }
                ],
                "symbols": [],
                "exports": [],
            },
            {
                "path": "src/controllers/attachment-controller.ts",
                "imports": [],
                "exports": [{"kind": "default", "symbol": "AttachmentController"}],
                "symbols": [
                    {"name": "AttachmentController", "kind": "class", "file": "src/controllers/attachment-controller.ts", "line": 1},
                    {
                        "name": "createTaskAttachment",
                        "qualified_name": "AttachmentController.createTaskAttachment",
                        "kind": "class_method",
                        "parent": "AttachmentController",
                        "file": "src/controllers/attachment-controller.ts",
                        "line": 10,
                        "static": True,
                        "async": True,
                    },
                ],
            },
        ]
    }
    resolved = resolve_handler_reference(
        _endpoint("safeControllerFunction(AttachmentController.createTaskAttachment)"),
        index,
    )
    assert resolved["resolved"] is True
    assert (
        resolved["primary_handler"]["symbol"]["qualified_name"]
        == "AttachmentController.createTaskAttachment"
    )


def test_resolve_top_level_local_function() -> None:
    index = {
        "files": [
            {
                "path": "src/routes/router.ts",
                "imports": [],
                "exports": [],
                "symbols": [
                    {
                        "name": "getList",
                        "kind": "function",
                        "file": "src/routes/router.ts",
                        "line": 5,
                    }
                ],
            }
        ]
    }
    resolved = resolve_handler_reference(_endpoint("getList"), index)
    assert resolved["resolved"] is True
    assert resolved["primary_handler"]["symbol"]["kind"] == "function"


def test_resolve_imported_function() -> None:
    index = {
        "files": [
            {
                "path": "src/routes/router.ts",
                "imports": [
                    {
                        "local": "getList",
                        "source": "../handlers",
                        "resolved_file": "src/handlers.ts",
                    }
                ],
                "exports": [],
                "symbols": [],
            },
            {
                "path": "src/handlers.ts",
                "imports": [],
                "exports": [{"kind": "named", "symbol": "getList"}],
                "symbols": [
                    {"name": "getList", "kind": "function", "file": "src/handlers.ts", "line": 2}
                ],
            },
        ]
    }
    resolved = resolve_handler_reference(_endpoint("getList"), index)
    assert resolved["resolved"] is True
    assert resolved["primary_handler"]["symbol"]["file"] == "src/handlers.ts"


def test_multiple_handler_candidates_primary_is_last() -> None:
    index = {
        "files": [
            {
                "path": "src/routes/router.ts",
                "imports": [
                    {"local": "imageToWebp", "source": "../img", "resolved_file": "src/img.ts"},
                    {
                        "local": "AttachmentController",
                        "source": "../controllers/attachment-controller",
                        "resolved_file": "src/controllers/attachment-controller.ts",
                    },
                ],
                "exports": [],
                "symbols": [],
            },
            {
                "path": "src/img.ts",
                "imports": [],
                "exports": [{"kind": "named", "symbol": "imageToWebp"}],
                "symbols": [{"name": "imageToWebp", "kind": "function", "file": "src/img.ts", "line": 1}],
            },
            {
                "path": "src/controllers/attachment-controller.ts",
                "imports": [],
                "exports": [{"kind": "default", "symbol": "AttachmentController"}],
                "symbols": [
                    {"name": "AttachmentController", "kind": "class", "file": "src/controllers/attachment-controller.ts", "line": 1},
                    {
                        "name": "createAvatarAttachment",
                        "qualified_name": "AttachmentController.createAvatarAttachment",
                        "kind": "class_method",
                        "parent": "AttachmentController",
                        "file": "src/controllers/attachment-controller.ts",
                        "line": 15,
                    },
                ],
            },
        ]
    }
    resolved = resolve_handler_reference(
        _endpoint(
            "taskAttachmentsValidator, safeControllerFunction(imageToWebp), safeControllerFunction(AttachmentController.createAvatarAttachment)"
        ),
        index,
    )
    assert resolved["resolved"] is True
    assert resolved["primary_handler"]["normalized_handler"] == "AttachmentController.createAvatarAttachment"
    assert any(item["normalized_handler"] == "imageToWebp" for item in resolved["prehandlers"])


def test_ambiguous_class_method_returns_candidates() -> None:
    index = {
        "files": [],
    }
    # emulate symbol map ambiguity via duplicate names in synthetic files
    index["files"] = [
        {
            "path": "a.ts",
            "imports": [],
            "exports": [],
            "symbols": [
                {
                    "name": "create",
                    "qualified_name": "AttachmentController.create",
                    "kind": "class_method",
                    "parent": "AttachmentController",
                    "file": "a.ts",
                    "line": 1,
                }
            ],
        },
        {
            "path": "b.ts",
            "imports": [],
            "exports": [],
            "symbols": [
                {
                    "name": "create",
                    "qualified_name": "AttachmentController.create",
                    "kind": "class_method",
                    "parent": "AttachmentController",
                    "file": "b.ts",
                    "line": 1,
                }
            ],
        },
    ]
    resolved = resolve_handler_reference(_endpoint("AttachmentController.create"), index)
    assert resolved["resolved"] is False
    assert resolved["unresolved_handlers"][0]["reason"] == "ambiguous"


def test_unresolved_handler_does_not_crash() -> None:
    resolved = resolve_handler_reference(_endpoint("MissingController.create"), {"files": []})
    assert resolved["resolved"] is False
    assert resolved["unresolved_handlers"][0]["reason"] in {"not_found", "ambiguous"}
    assert "diagnostics" in resolved["unresolved_handlers"][0]


def test_directory_import_resolution_flow() -> None:
    index = {
        "files": [
            {
                "path": "src/routes/router.ts",
                "imports": [
                    {
                        "local": "ApiController",
                        "source": "./controllers",
                        "resolved_file": "src/routes/controllers/index.ts",
                    }
                ],
                "exports": [],
                "symbols": [],
            },
            {
                "path": "src/routes/controllers/index.ts",
                "imports": [],
                "exports": [{"kind": "default", "symbol": "ApiController"}],
                "symbols": [
                    {"name": "ApiController", "kind": "class", "file": "src/routes/controllers/index.ts", "line": 1},
                    {
                        "name": "list",
                        "qualified_name": "ApiController.list",
                        "kind": "class_method",
                        "parent": "ApiController",
                        "file": "src/routes/controllers/index.ts",
                        "line": 4,
                    },
                ],
            },
        ]
    }
    resolved = resolve_handler_reference(_endpoint("ApiController.list"), index)
    assert resolved["resolved"] is True
    assert resolved["primary_handler"]["symbol"]["qualified_name"] == "ApiController.list"


def test_route_declaration_snippet_preserves_middleware_and_prehandler_order() -> None:
    endpoint = EndpointCandidate(
        method="POST",
        path="/avatar",
        handler="AttachmentController.createAvatarAttachment",
        file="src/routes/router.ts",
        repo="api",
        evidence=[
            {
                "file": "src/routes/router.ts",
                "label": "route_declaration",
                "snippet": 'attachmentsApiRouter.post("/avatar", avatarValidator, safeControllerFunction(imageToWebp), safeControllerFunction(AttachmentController.createAvatarAttachment));',
            }
        ],
    )
    index = {
        "files": [
            {
                "path": "src/routes/router.ts",
                "imports": [
                    {"local": "imageToWebp", "source": "../img", "resolved_file": "src/img.ts"},
                    {"local": "AttachmentController", "source": "../controllers/a", "resolved_file": "src/controllers/a.ts"},
                ],
                "exports": [],
                "symbols": [],
            },
            {
                "path": "src/img.ts",
                "imports": [],
                "exports": [{"kind": "named", "symbol": "imageToWebp"}],
                "symbols": [{"name": "imageToWebp", "kind": "function", "file": "src/img.ts", "line": 1}],
            },
            {
                "path": "src/controllers/a.ts",
                "imports": [],
                "exports": [{"kind": "default", "symbol": "AttachmentController"}],
                "symbols": [
                    {"name": "AttachmentController", "kind": "class", "file": "src/controllers/a.ts", "line": 1},
                    {
                        "name": "createAvatarAttachment",
                        "qualified_name": "AttachmentController.createAvatarAttachment",
                        "kind": "class_method",
                        "parent": "AttachmentController",
                        "file": "src/controllers/a.ts",
                        "line": 10,
                    },
                ],
            },
        ]
    }
    resolved = resolve_handler_reference(endpoint, index)
    assert resolved["resolved"] is True
    assert resolved["primary_handler"]["normalized_handler"] == "AttachmentController.createAvatarAttachment"
    pre_names = [item.get("normalized_handler") for item in resolved["prehandlers"]]
    assert "avatarValidator" in pre_names
    assert "imageToWebp" in pre_names


def test_resolver_matches_by_parent_and_name_when_qualified_missing() -> None:
    index = {
        "files": [
            {
                "path": "src/routes/router.ts",
                "imports": [
                    {
                        "local": "AttachmentController",
                        "source": "../controllers/attachment-controller",
                        "resolved_file": "src/controllers/attachment-controller.ts",
                    }
                ],
                "exports": [],
                "symbols": [],
            },
            {
                "path": "src/controllers/attachment-controller.ts",
                "imports": [],
                "exports": [{"kind": "default", "symbol": "AttachmentController"}],
                "symbols": [
                    {"name": "AttachmentController", "kind": "class", "file": "src/controllers/attachment-controller.ts", "line": 1},
                    {
                        "name": "createTaskAttachment",
                        "kind": "class_method",
                        "parent": "AttachmentController",
                        "file": "src/controllers/attachment-controller.ts",
                        "line": 12,
                        "start_line": 12,
                        "end_line": 20,
                        "static": True,
                        "async": True,
                    },
                ],
            },
        ]
    }
    resolved = resolve_handler_reference(_endpoint("AttachmentController.createTaskAttachment"), index)
    assert resolved["resolved"] is True
    assert resolved["primary_handler"]["symbol"]["qualified_name"] == "createTaskAttachment"


def test_resolver_supports_wrapped_index_payload_shape() -> None:
    wrapped_index = {
        "timestamp": "2026-05-25T00:00:00Z",
        "index": {
            "files": [
                {
                    "path": "src/routes/router.ts",
                    "imports": [
                        {
                            "local": "AttachmentController",
                            "source": "../controllers/attachment-controller",
                            "resolved_file": "src/controllers/attachment-controller.ts",
                        }
                    ],
                    "exports": [],
                    "symbols": [],
                },
                {
                    "path": "src/controllers/attachment-controller.ts",
                    "imports": [],
                    "exports": [{"kind": "default", "symbol": "AttachmentController"}],
                    "symbols": [
                        {"name": "AttachmentController", "kind": "class", "file": "src/controllers/attachment-controller.ts", "line": 1},
                        {
                            "name": "createTaskAttachment",
                            "qualified_name": "AttachmentController.createTaskAttachment",
                            "kind": "class_method",
                            "parent": "AttachmentController",
                            "file": "src/controllers/attachment-controller.ts",
                            "line": 10,
                            "start_line": 10,
                            "end_line": 22,
                            "static": True,
                            "async": True,
                        },
                    ],
                },
            ]
        },
    }
    resolved = resolve_handler_reference(_endpoint("AttachmentController.createTaskAttachment"), wrapped_index)
    assert resolved["resolved"] is True
    assert resolved["primary_handler"]["symbol"]["qualified_name"] == "AttachmentController.createTaskAttachment"
