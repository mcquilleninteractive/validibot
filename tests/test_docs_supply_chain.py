"""Guard the developer-documentation browser-asset supply chain.

Documentation diagrams are inert, checked-in SVG files, while fonts come from
exact npm pins and are served locally. These tests keep the static-diagram,
font, and no-CDN contracts from silently drifting during documentation or
dependency upgrades.
"""

import json
import re
import unittest
from pathlib import Path

from defusedxml import ElementTree as SafeElementTree

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_JSON = REPOSITORY_ROOT / "package.json"
DOCS_ROOT = REPOSITORY_ROOT / "docs" / "dev_docs"
DIAGRAM_DIRECTORY = DOCS_ROOT / "images" / "diagrams"
STATIC_DIAGRAMS = (
    "basic-validation-run.svg",
    "cloud-run-validator-flow.svg",
    "docker-validator-flow.svg",
    "repository-architecture-overview.svg",
    "repository-architecture.svg",
    "workflow-execution-lifecycle.svg",
)
CDN_LIBRARY_HOST_PATTERN = re.compile(
    rb"(?:unpkg\.com|cdn\.jsdelivr\.net|cdnjs\.cloudflare\.com"
    rb"|fonts\.googleapis\.com|fonts\.gstatic\.com)",
    re.IGNORECASE,
)


class DeveloperDocsSupplyChainTests(unittest.TestCase):
    """Enforce inert diagrams and locally served documentation assets."""

    def test_diagrams_require_no_browser_runtime(self) -> None:
        """Static diagrams must not reintroduce an executable renderer."""
        package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        self.assertNotIn("mermaid", package["devDependencies"])
        self.assertFalse(
            (DOCS_ROOT / "javascripts" / "vendor" / "mermaid.min.js").exists(),
        )

        markdown_sources = [REPOSITORY_ROOT / "README.md", *DOCS_ROOT.rglob("*.md")]
        for path in markdown_sources:
            self.assertNotIn("```mermaid", path.read_text(encoding="utf-8"))

    def test_static_diagrams_are_accessible_and_self_contained(self) -> None:
        """Every diagram must be accessible without scripts or remote assets."""
        self.assertEqual(
            sorted(path.name for path in DIAGRAM_DIRECTORY.glob("*.svg")),
            list(STATIC_DIAGRAMS),
        )

        svg_namespace = "{http://www.w3.org/2000/svg}"
        for name in STATIC_DIAGRAMS:
            path = DIAGRAM_DIRECTORY / name
            root = SafeElementTree.parse(path).getroot()
            self.assertEqual(root.tag, f"{svg_namespace}svg")
            self.assertEqual(root.attrib.get("role"), "img")
            self.assertTrue(root.attrib.get("viewBox"))

            labelled_by = set(root.attrib.get("aria-labelledby", "").split())
            children_by_id = {
                child.attrib["id"]: child for child in root if "id" in child.attrib
            }
            self.assertEqual(len(labelled_by), 2)
            self.assertTrue(labelled_by.issubset(children_by_id))
            self.assertTrue(
                any(child.tag == f"{svg_namespace}title" for child in root),
            )
            self.assertTrue(
                any(child.tag == f"{svg_namespace}desc" for child in root),
            )
            self.assertFalse(
                any(node.tag == f"{svg_namespace}script" for node in root.iter()),
            )
            for node in root.iter():
                for attribute in (
                    "href",
                    "{http://www.w3.org/1999/xlink}href",
                ):
                    self.assertFalse(
                        node.attrib.get(attribute, "").startswith(
                            ("http://", "https://")
                        ),
                    )

    def test_fonts_are_exactly_pinned_and_vendored(self) -> None:
        """Docs typography must not depend on mutable Google Fonts responses."""
        package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        font_packages = (
            "@fontsource-variable/inter",
            "@fontsource-variable/jetbrains-mono",
            "@fontsource-variable/space-grotesk",
        )
        for name in font_packages:
            self.assertRegex(
                package["devDependencies"][name],
                r"^\d+\.\d+\.\d+$",
            )

        font_directory = DOCS_ROOT / "fonts"
        self.assertGreater(
            (font_directory / "inter-latin-wght-normal.woff2").stat().st_size,
            10_000,
        )
        self.assertGreater(
            (font_directory / "jetbrains-mono-latin-wght-normal.woff2").stat().st_size,
            10_000,
        )
        self.assertGreater(
            (font_directory / "space-grotesk-latin-wght-normal.woff2").stat().st_size,
            10_000,
        )

    def test_theme_disables_remote_font_generation(self) -> None:
        """Zensical must not emit its default Google Fonts stylesheet links."""
        configuration = (REPOSITORY_ROOT / "mkdocs.dev.yml").read_text(
            encoding="utf-8",
        )
        self.assertIn("font: false", configuration)
        self.assertIn("- stylesheets/fonts.css", configuration)

    def test_docs_source_has_no_public_cdn_library_hosts(self) -> None:
        """Executable docs libraries must be served by Validibot itself."""
        offenders: list[str] = []
        for path in [REPOSITORY_ROOT / "mkdocs.dev.yml", *DOCS_ROOT.rglob("*")]:
            if not path.is_file():
                continue
            if CDN_LIBRARY_HOST_PATTERN.search(path.read_bytes()):
                offenders.append(str(path.relative_to(REPOSITORY_ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
