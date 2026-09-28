"""Java structural extractor -- the shared symbol index had no Java adapter
at all (`_extractor_registry()` listed only JS/TS and Python), so any
changed `.java` file yielded zero symbols regardless of content, breaking
route -> handler -> downstream in the middle for every Java repository.

The scope is deliberately narrow: regex + brace-depth tracking, the same
approach the JS/TS adapter uses, not a full parser (see the module
docstring in `sydes.trace.handler_symbols.java`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.trace.handler_symbols.java import JavaHandlerSymbolExtractor, resolve_java_import


@pytest.fixture()
def extractor() -> JavaHandlerSymbolExtractor:
    return JavaHandlerSymbolExtractor()


def _extract(extractor: JavaHandlerSymbolExtractor, name: str, source: str) -> dict:
    return extractor.extract_file(Path("/repo"), name, source).to_dict()


def _symbol(payload: dict, name: str, kind: str | None = None) -> dict:
    return next(
        item
        for item in payload["symbols"]
        if item["name"] == name and (kind is None or item["kind"] == kind)
    )


_MONITOR_SERVICE = '''package com.xkcoding.rbac.security.service;

import com.xkcoding.rbac.security.util.RedisUtil;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.stream.Collectors;

@Slf4j
@Service
public class MonitorService {
    @Autowired
    private RedisUtil redisUtil;

    public PageResult<OnlineUser> onlineUser(PageCondition pageCondition) {
        return null;
    }

    public void kickout(List<String> names) {
        List<String> distinctNames = names.stream().filter(StrUtil::isNotBlank).distinct().collect(Collectors.toList());
        redisUtil.delete(distinctNames);
    }
}
'''


def test_extracts_class_and_methods_with_correct_spans(extractor: JavaHandlerSymbolExtractor) -> None:
    payload = _extract(extractor, "MonitorService.java", _MONITOR_SERVICE)
    names = [item["name"] for item in payload["symbols"]]
    assert names == ["MonitorService", "onlineUser", "kickout"]

    cls = _symbol(payload, "MonitorService", kind="class")
    assert cls["kind"] == "class"
    assert cls["decorators"] == ["Slf4j", "Service"]
    assert cls["start_line"] == 11
    assert cls["end_line"] == _MONITOR_SERVICE.rstrip("\n").count("\n") + 1

    kickout = _symbol(payload, "kickout")
    assert kickout["kind"] == "class_method"
    assert kickout["parent"] == "MonitorService"
    assert kickout["qualified_name"] == "MonitorService.kickout"
    assert kickout["signature"] == "kickout(List<String> names)"
    assert kickout["start_line"] == 19
    # The body ends at the closing brace of `kickout`, not the class's own.
    assert kickout["end_line"] == 22


def test_extracts_package_and_import_kinds(extractor: JavaHandlerSymbolExtractor) -> None:
    payload = _extract(extractor, "MonitorService.java", _MONITOR_SERVICE)
    package_entries = [item for item in payload["imports"] if item["kind"] == "package"]
    assert package_entries[0]["source"] == "com.xkcoding.rbac.security.service"

    named = {item["imported"] for item in payload["imports"] if item["kind"] == "named"}
    assert "com.xkcoding.rbac.security.util.RedisUtil" in named
    assert "org.springframework.stereotype.Service" in named


def test_wildcard_and_static_imports(extractor: JavaHandlerSymbolExtractor) -> None:
    source = "\n".join(
        [
            "package com.example;",
            "import java.util.*;",
            "import static org.junit.jupiter.api.Assertions.assertEquals;",
            "public class Foo {}",
        ]
    )
    payload = _extract(extractor, "Foo.java", source)
    assert not any(item["kind"] == "named" and item["source"] == "java.util.*" for item in payload["imports"])
    static_imports = [item for item in payload["imports"] if item["kind"] == "static"]
    assert static_imports and static_imports[0]["source"] == "org.junit.jupiter.api.Assertions.assertEquals"


def test_constructor_is_recognized_by_name_matching_enclosing_class(
    extractor: JavaHandlerSymbolExtractor,
) -> None:
    source = "\n".join(
        [
            "package com.example;",
            "public class Widget {",
            "    private final int size;",
            "",
            "    public Widget(int size) {",
            "        this.size = size;",
            "    }",
            "",
            "    public int getSize() {",
            "        return size;",
            "    }",
            "}",
        ]
    )
    payload = _extract(extractor, "Widget.java", source)
    ctor = _symbol(payload, "Widget", kind="class_method")
    assert ctor["parent"] == "Widget"
    getter = _symbol(payload, "getSize")
    assert getter["kind"] == "class_method"


def test_control_flow_keywords_are_never_mistaken_for_methods(
    extractor: JavaHandlerSymbolExtractor,
) -> None:
    source = "\n".join(
        [
            "package com.example;",
            "public class Widget {",
            "    public void run(int x) {",
            "        if (x > 0) {",
            "            for (int i = 0; i < x; i++) {",
            "                System.out.println(i);",
            "            }",
            "        } else if (x < 0) {",
            "            throw new IllegalArgumentException();",
            "        }",
            "    }",
            "}",
        ]
    )
    payload = _extract(extractor, "Widget.java", source)
    names = [item["name"] for item in payload["symbols"]]
    assert names == ["Widget", "run"]
    run = _symbol(payload, "run")
    assert run["end_line"] == 11


def test_nested_class_gets_qualified_name_and_own_span(
    extractor: JavaHandlerSymbolExtractor,
) -> None:
    source = "\n".join(
        [
            "package com.example;",
            "public class Outer {",
            "    public void outerMethod() {",
            "    }",
            "",
            "    private static class Inner {",
            "        void innerMethod() {",
            "        }",
            "    }",
            "}",
        ]
    )
    payload = _extract(extractor, "Outer.java", source)
    inner = _symbol(payload, "Inner")
    assert inner["parent"] == "Outer"
    assert inner["qualified_name"] == "Outer.Inner"
    inner_method = _symbol(payload, "innerMethod")
    assert inner_method["parent"] == "Inner"
    assert inner_method["qualified_name"] == "Inner.innerMethod"


def test_junit_test_annotation_is_captured_as_a_decorator(
    extractor: JavaHandlerSymbolExtractor,
) -> None:
    source = "\n".join(
        [
            "package com.example;",
            "public class WidgetTest {",
            "    @Test",
            "    void kickoutFiltersBlankNames() {",
            "    }",
            "}",
        ]
    )
    payload = _extract(extractor, "WidgetTest.java", source)
    method = _symbol(payload, "kickoutFiltersBlankNames")
    assert method["decorators"] == ["Test"]


def test_resolve_java_import_finds_sibling_class_under_src_main_java(tmp_path: Path) -> None:
    root = tmp_path
    pkg_dir = root / "module" / "src" / "main" / "java" / "com" / "example" / "service"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "Widget.java").write_text("package com.example.service;\npublic class Widget {}\n")

    resolved = resolve_java_import(root, "com.example.service.Widget")
    assert resolved == "module/src/main/java/com/example/service/Widget.java"


def test_resolve_java_import_returns_none_for_third_party_class(tmp_path: Path) -> None:
    root = tmp_path
    (root / "src" / "main" / "java").mkdir(parents=True)
    assert resolve_java_import(root, "org.springframework.stereotype.Service") is None
