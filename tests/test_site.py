"""The Cloudflare page publishes docs/ only: nothing but the page and the documents it renders."""
import json
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_wrangler_publishes_docs_only():
    with open(os.path.join(ROOT, "wrangler.jsonc"), encoding="utf-8") as f:
        cfg = json.loads(re.sub(r"^\s*//.*$", "", f.read(), flags=re.M))
    assert cfg["assets"]["directory"] == "./docs"
    assert sorted(os.listdir(os.path.join(ROOT, "docs"))) == ["GUIDE.md", "PREREG-stage2-eurusd.md", "index.html", "vendor"]
    assert all(n.startswith(("marked-", "mermaid-", "LICENSE-")) for n in os.listdir(os.path.join(ROOT, "docs", "vendor")))


def test_page_loads_its_scripts_locally():
    with open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8") as f:
        html = f.read()
    for src in re.findall(r'<script src="([^"]+)"', html):
        assert os.path.exists(os.path.join(ROOT, "docs", src)), src


def test_page_tabs_point_at_existing_documents():
    with open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8") as f:
        html = f.read()
    for name in re.findall(r'data-doc="([^"]+)"', html):
        assert os.path.exists(os.path.join(ROOT, "docs", name)), name
