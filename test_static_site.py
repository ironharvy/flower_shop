from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urlparse

import pytest


ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
AIORCHESTRA_CONFIG = ROOT / ".aiorchestra" / "config.yaml"
DISALLOWED_PACKAGE_OR_BUILD_FILES = {
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "vite.config.js",
    "webpack.config.js",
    "node_modules",
    "dist",
    "build",
}
VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def require(condition, message):
    if not condition:
        pytest.fail(message)


class StaticSiteParser(HTMLParser):
    HIDDEN_CONTEXT = {"head", "script", "style", "title", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.ids = set()
        self.links = []
        self.assets = []
        self.title_parts = []
        self.style_parts = []
        self.visible_parts = []
        self._stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag not in VOID_ELEMENTS:
            self._stack.append(tag)

        if attrs.get("id"):
            self.ids.add(attrs["id"])

        if tag == "link":
            self.links.append(attrs)

        for attr in ("href", "src", "srcset", "poster", "action", "style"):
            if attrs.get(attr):
                self.assets.append((tag, attr, attrs[attr]))

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data):
        if self._stack and self._stack[-1] == "style":
            self.style_parts.append(data)

        text = " ".join(data.split())
        if not text:
            return

        if self._stack and self._stack[-1] == "title":
            self.title_parts.append(text)

        if not self.HIDDEN_CONTEXT.intersection(self._stack):
            self.visible_parts.append(text)

    @property
    def title(self):
        return " ".join(self.title_parts)

    @property
    def visible_text(self):
        return " ".join(self.visible_parts)

    @property
    def inline_styles(self):
        return "\n".join(self.style_parts)


@pytest.fixture(scope="module")
def html_text():
    require(INDEX.exists(), "index.html should exist at the repository root")
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parsed_site(html_text):
    parser = StaticSiteParser()
    parser.feed(html_text)
    return parser


def local_path_from_url(value):
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or value.startswith(("#", "mailto:", "tel:")):
        return None
    path = (ROOT / parsed.path).resolve()
    try:
        path.relative_to(ROOT)
    except ValueError:
        return None
    return path


def class_tokens(attrs):
    return set(attrs.get("class", "").split())


def is_external_url(value):
    return value.startswith("//") or urlparse(value).scheme in {"http", "https"}


def external_asset_references(tag, attr, value):
    if attr == "style":
        return []
    if attr == "srcset":
        references = []
        for candidate in value.split(","):
            parts = candidate.strip().split(maxsplit=1)
            if not parts:
                continue
            url = parts[0]
            if is_external_url(url):
                references.append(f"{tag}[{attr}]={url!r}")
        return references
    if is_external_url(value):
        return [f"{tag}[{attr}]={value!r}"]
    return []


def external_url_references(text):
    candidates = re.findall(r"url\(\s*['\"]?([^'\")]+)", text)
    candidates.extend(re.findall(r"@import\s+['\"]([^'\"]+)", text))
    return [value for value in candidates if is_external_url(value)]


def test_aiorchestra_runs_pytest():
    require(
        AIORCHESTRA_CONFIG.exists(),
        ".aiorchestra/config.yaml should exist so AIOrchestra can run validation",
    )
    config_text = AIORCHESTRA_CONFIG.read_text(encoding="utf-8")
    require(
        re.search(r"(?m)^\s*test_command\s*:\s*pytest\s*$", config_text),
        ".aiorchestra/config.yaml should set test_command to pytest",
    )


def test_index_links_existing_local_css(parsed_site):
    stylesheets = [
        link
        for link in parsed_site.links
        if link.get("rel") and "stylesheet" in link["rel"].lower().split()
    ]
    require(stylesheets, "index.html should link to a local stylesheet")

    missing = []
    for link in stylesheets:
        href = link.get("href", "")
        path = local_path_from_url(href)
        if path is None:
            missing.append(f"{href!r} is not a local CSS file")
        elif path.suffix != ".css" or not path.is_file():
            missing.append(f"{href!r} does not resolve to an existing CSS file")

    require(
        not missing,
        "Stylesheet links should point to existing local CSS files: "
        + ", ".join(missing),
    )


def test_brand_appears_in_title_or_visible_content(parsed_site):
    content = f"{parsed_site.title} {parsed_site.visible_text}"
    require(
        "Bloom & Branch" in content,
        "Page title or visible content should include 'Bloom & Branch'",
    )


@pytest.mark.parametrize(
    ("section_name", "candidates"),
    [
        ("bouquets/products", {"bouquets", "products"}),
        ("story/about", {"story", "about"}),
        ("contact/order", {"contact", "order"}),
    ],
)
def test_required_sections_or_anchors_exist(parsed_site, section_name, candidates):
    anchors = {
        attrs["href"].lstrip("#")
        for tag, attrs in parsed_site.tags
        if tag == "a" and attrs.get("href", "").startswith("#")
    }
    available = parsed_site.ids | anchors
    require(candidates & available, f"Missing section or anchor for {section_name}")


def test_product_entries_and_prices_are_present(parsed_site, html_text):
    product_entries = [
        attrs
        for tag, attrs in parsed_site.tags
        if {"product-card", "bouquet-card", "card"} & class_tokens(attrs)
    ]
    require(
        len(product_entries) >= 3,
        "Expected at least 3 product or bouquet card entries",
    )

    prices = re.findall(r"\$\d+(?:\.\d{2})?\b", html_text)
    require(
        len(prices) >= 3,
        "Expected at least 3 prices in dollar format, such as $38",
    )


def test_contact_details_are_complete(parsed_site):
    phone_links = [
        value
        for tag, attr, value in parsed_site.assets
        if tag == "a" and attr == "href" and value.startswith("tel:")
    ]
    email_links = [
        value
        for tag, attr, value in parsed_site.assets
        if tag == "a" and attr == "href" and value.startswith("mailto:")
    ]
    visible = parsed_site.visible_text.lower()
    visible_lines = [part.lower() for part in parsed_site.visible_parts]
    address_pattern = re.compile(
        r"\d+\s+\w+.*\b(lane|street|st|avenue|ave|road|rd|drive|dr)\b"
    )

    require(phone_links, "Contact details should include a phone link with a tel: href")
    require(email_links, "Contact details should include an email link with a mailto: href")
    require(
        "hours" in visible
        or re.search(r"\b(mon|tue|wed|thu|fri|sat|sun)\b", visible),
        "Contact details should include shop hours text",
    )
    require(
        "address" in visible
        or any(address_pattern.search(line) for line in visible_lines),
        "Contact details should include address text",
    )


def test_no_external_urls_are_referenced(parsed_site):
    external_assets = []
    for tag, attr, value in parsed_site.assets:
        external_assets.extend(external_asset_references(tag, attr, value))
    inline_style_urls = []
    for tag, attr, value in parsed_site.assets:
        if attr == "style":
            inline_style_urls.extend(
                f"{tag}[style]={url!r}" for url in external_url_references(value)
            )
    external_css_urls = external_url_references(parsed_site.inline_styles)

    require(
        not external_assets,
        "No external CDN, framework, font, or image URLs should be referenced: "
        + ", ".join(external_assets),
    )
    require(
        not inline_style_urls,
        "No external URLs should be referenced from inline style attributes: "
        + ", ".join(inline_style_urls),
    )
    require(
        not external_css_urls,
        "No external URLs should be referenced from inline CSS: "
        + ", ".join(external_css_urls),
    )


def test_referenced_local_css_files_do_not_use_external_urls(parsed_site):
    css_paths = [
        local_path_from_url(link.get("href", ""))
        for link in parsed_site.links
        if link.get("rel") and "stylesheet" in link["rel"].lower().split()
    ]
    external_urls = []
    for css_path in filter(None, css_paths):
        if not css_path.exists():
            continue
        css_text = css_path.read_text(encoding="utf-8")
        external_urls.extend(
            f"{css_path.name}: {value}" for value in external_url_references(css_text)
        )

    require(
        not external_urls,
        "Local CSS should not reference external URLs: " + ", ".join(external_urls),
    )


def test_validation_does_not_require_package_manager_or_build_artifacts():
    present = sorted(
        path.name
        for path in ROOT.iterdir()
        if path.name in DISALLOWED_PACKAGE_OR_BUILD_FILES
    )
    require(
        not present,
        "Validation should stay static and pytest-only; remove package/build artifacts: "
        + ", ".join(present),
    )
