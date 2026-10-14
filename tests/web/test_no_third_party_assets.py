"""
Guard: opening the approval UI makes no third-party request (#184).

The UI used to pull Inter/Montserrat from Google Fonts, so every page load sent
the operator's IP + user agent to fonts.googleapis.com / fonts.gstatic.com and
the CSP had to allow both. Fonts are now vendored under web/public/fonts. These
tests fail CI if any of the three places that could quietly reintroduce an
external fetch drifts:

    web/index.html ──── no external origin in any src= / href=
    web/src/index.css ─ every url() is a root-relative path that exists
                        in web/public (so a typo can't fall back to a CDN
                        fix later), and both UI font families are declared
    web/nginx.conf ──── CSP font-src is exactly 'self'; no Google font origin
                        anywhere in the policy

These are Python tests on purpose: CI's `test` job runs pytest, not vitest.
Plausible is out of scope here: it is injected at runtime only when the
operator sets VITE_PLAUSIBLE_DOMAIN *and* the user consents.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"
INDEX_HTML = WEB / "index.html"
INDEX_CSS = WEB / "src" / "index.css"
NGINX_CONF = WEB / "nginx.conf"
PUBLIC = WEB / "public"
FONTS = PUBLIC / "fonts"

# scheme:// or protocol-relative //host — anything that leaves the origin.
_EXTERNAL = re.compile(r"^\s*(?:[a-z][a-z0-9+.-]*:)?//", re.IGNORECASE)


class _AssetRefs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"src", "href", "srcset", "poster", "data"} and value:
                self.refs.append((tag, name, value))


def _html_refs() -> list[tuple[str, str, str]]:
    parser = _AssetRefs()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))
    return parser.refs


def _css_urls() -> list[str]:
    css = INDEX_CSS.read_text(encoding="utf-8")
    return [m.strip("'\" ") for m in re.findall(r"url\(([^)]*)\)", css)]


def _csp() -> str:
    conf = NGINX_CONF.read_text(encoding="utf-8")
    match = re.search(r'^\s*default\s+"([^"]*)";', conf, re.MULTILINE)
    assert match, "could not find the CSP string in the $csp_header map"
    return match.group(1)


def _csp_directive(csp: str, name: str) -> str:
    for part in csp.split(";"):
        tokens = part.split()
        if tokens and tokens[0] == name:
            return " ".join(tokens[1:])
    raise AssertionError(f"CSP has no {name} directive")


def test_index_html_references_no_external_origin() -> None:
    refs = _html_refs()
    assert refs, "parser found no src/href at all; did index.html move?"
    external = [r for r in refs if _EXTERNAL.match(r[2])]
    assert external == [], f"index.html loads from another origin: {external}"


def test_index_css_urls_are_local_files() -> None:
    urls = _css_urls()
    assert urls, "expected @font-face url()s in index.css"
    for url in urls:
        assert not _EXTERNAL.match(url), f"index.css loads from another origin: {url}"
        assert url.startswith("/"), f"use a root-relative /public path, got {url}"
        assert (PUBLIC / url.lstrip("/")).is_file(), f"{url} does not exist under web/public"


def test_ui_font_families_are_self_hosted() -> None:
    # 'Inter' is the body font (index.css), 'Montserrat' the display font
    # (tailwind.config.js). Each needs at least one local @font-face.
    css = INDEX_CSS.read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*{([^}]*)}", css)
    families = {m for face in faces for m in re.findall(r"font-family:\s*'([^']+)'", face)}
    assert {"Inter", "Montserrat"} <= families


def test_vendored_fonts_ship_their_license() -> None:
    # SIL OFL 1.1 requires the license to accompany redistributed font files.
    woff2 = sorted(p.name for p in FONTS.glob("*.woff2"))
    assert woff2, "no vendored fonts found"
    for name in woff2:
        family = name.split("-", 1)[0].capitalize()
        license_file = FONTS / f"{family}-OFL.txt"
        assert license_file.is_file(), f"{name} has no {license_file.name}"
        assert "SIL Open Font License" in license_file.read_text(encoding="utf-8")


def test_csp_allows_no_google_font_origin() -> None:
    csp = _csp()
    assert "fonts.googleapis.com" not in csp
    assert "fonts.gstatic.com" not in csp
    assert _csp_directive(csp, "font-src") == "'self'"
