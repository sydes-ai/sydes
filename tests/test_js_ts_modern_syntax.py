"""Modern JS/TS syntax the structural extractor must recover.

Every case here is reduced from real files in `anuraghazra/github-readme-stats`,
the one backend service the Multi-SWE-bench survey found applicable. On that
repository Sydes discovered the Express routes correctly but produced no
affected flows, because the files holding the route handlers yielded no symbols
at all — breaking route -> handler -> downstream in the middle.

The scope is deliberately narrow: the concrete constructs these files use, not
a JavaScript parser.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.trace.handler_symbols.js_ts import JsTsHandlerSymbolExtractor


@pytest.fixture()
def extractor() -> JsTsHandlerSymbolExtractor:
    return JsTsHandlerSymbolExtractor()


def _extract(extractor: JsTsHandlerSymbolExtractor, name: str, source: str) -> dict:
    return extractor.extract_file(Path("/repo"), name, source).to_dict()


def _names(payload: dict) -> list[str]:
    return [item["name"] for item in payload["symbols"]]


def _locals(payload: dict) -> list[str]:
    return [item["local"] for item in payload["imports"]]


# --------------------------------------------------------------------------
# F1 — CommonJS interop: destructured require spanning lines
# --------------------------------------------------------------------------

_MULTILINE_REQUIRE = '''const {
  kFormatter,
  encodeHTML,
  getCardColors,
} = require("../src/utils");
const icons = require("./icons");

const renderRepoCard = (repo, options = {}) => {
  return kFormatter(repo.stars);
};

module.exports = renderRepoCard;
'''


def test_multiline_destructured_require_yields_imports(extractor) -> None:
    """A require split over lines still binds every destructured name."""
    payload = _extract(extractor, "src/renderRepoCard.js", _MULTILINE_REQUIRE)

    assert "kFormatter" in _locals(payload)
    assert "encodeHTML" in _locals(payload)
    assert "getCardColors" in _locals(payload)
    sources = {item["local"]: item["source"] for item in payload["imports"]}
    assert sources["kFormatter"] == "../src/utils"


def test_single_line_require_still_works(extractor) -> None:
    """The pre-existing single-line form is unaffected."""
    payload = _extract(extractor, "a.js", _MULTILINE_REQUIRE)

    assert "icons" in _locals(payload)


# --------------------------------------------------------------------------
# F2 — ESM: named import spanning lines
# --------------------------------------------------------------------------

_MULTILINE_IMPORT = '''import { renderStatsCard } from "../src/cards/stats-card.js";
import {
  clampValue,
  CONSTANTS,
  parseArray,
  renderError,
} from "../src/common/utils.js";
import { fetchStats } from "../src/fetchers/stats-fetcher.js";

export const handler = () => clampValue(1, 2, 3);
'''


def test_multiline_named_import_yields_imports(extractor) -> None:
    """A braced import list split over lines binds each specifier."""
    payload = _extract(extractor, "api/index.js", _MULTILINE_IMPORT)

    for name in ("clampValue", "CONSTANTS", "parseArray", "renderError"):
        assert name in _locals(payload), f"{name} was not bound"
    sources = {item["local"]: item["source"] for item in payload["imports"]}
    assert sources["clampValue"] == "../src/common/utils.js"


def test_single_line_named_imports_still_work(extractor) -> None:
    """Neighbouring single-line imports keep working."""
    payload = _extract(extractor, "api/index.js", _MULTILINE_IMPORT)

    assert "renderStatsCard" in _locals(payload)
    assert "fetchStats" in _locals(payload)


# --------------------------------------------------------------------------
# F3 — anonymous default-exported handler (the route handler itself)
# --------------------------------------------------------------------------

_ESM_ANON_DEFAULT = '''import { fetchStats } from "../src/fetchers/stats-fetcher.js";

export default async (req, res) => {
  const stats = await fetchStats(req.query.username);
  res.send(stats);
};
'''

_CJS_ANON_DEFAULT = '''const fetchStats = require("../src/fetchStats");

module.exports = async (req, res) => {
  const stats = await fetchStats(req.query.username);
  res.send(stats);
};
'''


@pytest.mark.parametrize(
    ("filename", "source"),
    [("api/index.js", _ESM_ANON_DEFAULT), ("api/pin.js", _CJS_ANON_DEFAULT)],
)
def test_anonymous_default_export_handler_is_a_symbol(extractor, filename, source) -> None:
    """The handler has no name of its own, but it is still the module's export.

    Without a symbol here there is nothing for a route to bind to, so the whole
    flow disappears even when route discovery succeeded.
    """
    payload = _extract(extractor, filename, source)

    defaults = [s for s in payload["symbols"] if s.get("export_kind") == "default"]
    assert defaults, f"no default-exported symbol recovered from {filename}"
    symbol = defaults[0]
    assert symbol["kind"] == "function"
    assert symbol["exported"] is True
    assert symbol["start_line"] >= 1
    assert symbol["end_line"] >= symbol["start_line"]


@pytest.mark.parametrize(
    ("filename", "source"),
    [("api/index.js", _ESM_ANON_DEFAULT), ("api/pin.js", _CJS_ANON_DEFAULT)],
)
def test_anonymous_handler_body_is_spanned(extractor, filename, source) -> None:
    """The span must cover the body, or downstream call following sees nothing."""
    payload = _extract(extractor, filename, source)
    symbol = [s for s in payload["symbols"] if s.get("export_kind") == "default"][0]

    body = source.splitlines()[symbol["start_line"] - 1 : symbol["end_line"]]
    assert any("fetchStats(" in line for line in body), "downstream call outside the span"


def test_named_default_export_still_works(extractor) -> None:
    """The already-supported named form keeps its name."""
    payload = _extract(extractor, "m.js", "export default function foo(a) { return a; }\n")

    assert "foo" in _names(payload)
    assert payload["symbols"][0]["export_kind"] == "default"


def test_module_exports_of_a_named_symbol_still_works(extractor) -> None:
    """`module.exports = name` must not be shadowed by the anonymous form."""
    payload = _extract(extractor, "m.js", _MULTILINE_REQUIRE)

    assert "renderRepoCard" in _names(payload)


# --------------------------------------------------------------------------
# F4 — TypeScript declaration files
# --------------------------------------------------------------------------

_DECLARATION = '''type ThemeNames = keyof typeof import("../../themes/index.js");
type RankIcon = "default" | "github";

export type CommonOptions = {
  title_color: string;
  theme: ThemeNames;
};

export interface CardOptions {
  width: number;
}
'''


def test_type_declarations_are_recorded_as_exports(extractor) -> None:
    """A `.d.ts` declares no runtime behavior, but its type exports are facts.

    Recovering them distinguishes "this file has no runtime symbols by design"
    from "this file could not be parsed".
    """
    payload = _extract(extractor, "src/cards/types.d.ts", _DECLARATION)

    exported = {item.get("symbol") for item in payload["exports"]}
    assert "CommonOptions" in exported
    assert "CardOptions" in exported


def test_type_declarations_do_not_invent_runtime_symbols(extractor) -> None:
    """A type alias is not callable and must not appear as a function."""
    payload = _extract(extractor, "src/cards/types.d.ts", _DECLARATION)

    assert [s for s in payload["symbols"] if s["kind"] == "function"] == []


# --------------------------------------------------------------------------
# Arrow bindings already modelled must keep working
# --------------------------------------------------------------------------


def test_destructured_arrow_parameters_still_parse(extractor) -> None:
    """`const f = ({ a, b = [] }) => {}` is used throughout the repository."""
    payload = _extract(
        extractor,
        "src/common/utils.js",
        "const flexLayout = ({ items, gap, direction, sizes = [] }) => {\n  return items;\n};\n",
    )

    assert "flexLayout" in _names(payload)
