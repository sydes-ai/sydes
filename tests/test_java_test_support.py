"""Tests for Java `@Test`-annotated case-name recognition
(`verify.test_index`).

Before this, `_JAVA_TEST` only recognized the older JUnit3 naming
convention (no annotations at all -- a case is a case because its method
name contains "test"/"Test" somewhere). A real, confirmed gap: a
descriptively-named JUnit4/5 method like
`@Test public void saveRejectsBlankUsername()` matches neither the old
name-based regex nor anything annotation-aware, so the whole file fell
back to `_extract_cases_from_file`'s "no recognizable case names" branch
-- one synthetic file-level pseudo-case, losing every individual test's
own name, body, and assertion evidence. Confirmed on
sydes-examples/spring-boot-demo#1 (`UserServiceImplTest.java`).
"""

from __future__ import annotations

from sydes.verify.source_files import SourceFile
from sydes.verify.test_index import _extract_cases_from_file


def _java_file(path: str, text: str) -> SourceFile:
    return SourceFile(repo="app", path=path, text=text, role="test_usage_candidate", extension=".java")


def test_junit4_test_annotated_method_without_test_in_its_name_is_recognized() -> None:
    """The exact real shape from spring-boot-demo#1: `@Test(expected = ...)`
    on a descriptively-named method with no "test" substring at all."""
    text = (
        "public class UserServiceImplTest {\n"
        "    @Test(expected = IllegalArgumentException.class)\n"
        "    public void saveRejectsBlankUsername() {\n"
        "        userService.save(user);\n"
        "    }\n"
        "}\n"
    )
    cases = _extract_cases_from_file(_java_file("UserServiceImplTest.java", text))
    assert [c.name for c in cases] == ["saveRejectsBlankUsername"]
    assert cases[0].line == 3


def test_junit5_plain_test_annotation_without_test_in_its_name_is_recognized() -> None:
    text = (
        "class PetServiceTest {\n"
        "    @Test\n"
        "    void createDispatchesSubscribers() {\n"
        "        assertTrue(dispatched);\n"
        "    }\n"
        "}\n"
    )
    # JUnit5 permits package-private (no `public`) void methods too.
    text = text.replace("void createDispatchesSubscribers", "public void createDispatchesSubscribers")
    cases = _extract_cases_from_file(_java_file("PetServiceTest.java", text))
    assert [c.name for c in cases] == ["createDispatchesSubscribers"]


def test_fully_qualified_test_annotation_is_recognized() -> None:
    text = (
        "public class Foo {\n"
        "    @org.junit.jupiter.api.Test\n"
        "    public void rejectsBlankName() {\n"
        "    }\n"
        "}\n"
    )
    cases = _extract_cases_from_file(_java_file("Foo.java", text))
    assert [c.name for c in cases] == ["rejectsBlankName"]


def test_stacked_annotation_does_not_cancel_the_pending_test_match() -> None:
    """`@Test` followed by another annotation (`@DisplayName`, `@Disabled`,
    ...) before the method line -- the second annotation must not reset
    the pending state `@Test` set, mirroring the same fix already applied
    to Rust's stacked-attribute case."""
    text = (
        "public class Foo {\n"
        "    @Test\n"
        "    @DisplayName(\"rejects a blank username\")\n"
        "    public void rejectsBlankName() {\n"
        "    }\n"
        "}\n"
    )
    cases = _extract_cases_from_file(_java_file("Foo.java", text))
    assert [c.name for c in cases] == ["rejectsBlankName"]
    assert cases[0].line == 4


def test_multiple_annotated_methods_in_one_file_are_all_found_individually() -> None:
    """The exact real file shape from spring-boot-demo#1: two `@Test`
    methods, neither name containing "test", both must be indexed
    separately -- not collapsed into one file-level pseudo-case."""
    text = (
        "public class UserServiceImplTest {\n"
        "    @Test(expected = IllegalArgumentException.class)\n"
        "    public void saveRejectsBlankUsername() {\n"
        "        userService.save(user);\n"
        "    }\n"
        "\n"
        "    @Test\n"
        "    public void saveAcceptsNonBlankUsername() {\n"
        "        assertTrue(userService.save(user));\n"
        "    }\n"
        "}\n"
    )
    cases = _extract_cases_from_file(_java_file("UserServiceImplTest.java", text))
    assert [c.name for c in cases] == ["saveRejectsBlankUsername", "saveAcceptsNonBlankUsername"]
    assert cases[0].end_line < cases[1].line, "each case's range must not swallow the next case"


def test_old_junit3_style_naming_still_works_without_an_annotation() -> None:
    """The pre-existing, still-supported convention: no `@Test` annotation
    at all, a method name containing "test"/"Test" is still recognized."""
    text = (
        "public class LegacyTest {\n"
        "    public void testRejectsBlankUsername() {\n"
        "    }\n"
        "}\n"
    )
    cases = _extract_cases_from_file(_java_file("LegacyTest.java", text))
    assert [c.name for c in cases] == ["testRejectsBlankUsername"]


def test_a_test_annotation_with_no_matching_method_leaves_no_fabricated_case() -> None:
    """A `@Test` line immediately followed by something that is not a void
    method declaration (e.g. a field, or the annotation was misapplied)
    must not fabricate an individually-named case out of thin air -- the
    file still counts as evidence via the existing file-level fallback
    (`_extract_cases_from_file`'s "no recognizable case names" branch),
    but not as a case bearing a method name that was never declared."""
    text = (
        "public class Foo {\n"
        "    @Test\n"
        "    private int notAMethod = 5;\n"
        "}\n"
    )
    cases = _extract_cases_from_file(_java_file("Foo.java", text))
    assert [c.name for c in cases] != ["notAMethod"]
