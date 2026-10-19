"""Text helpers shared by collectors."""

from __future__ import annotations

from bs4 import BeautifulSoup

_BLOCKS = ["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"]


def html_to_text(html: str | None) -> str | None:
    """Readable plain text: block elements become lines, list items get "- "."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert(0, "- ")
    for tag in soup.find_all(_BLOCKS):
        tag.insert_after("\n")
    lines = (" ".join(line.split()) for line in soup.get_text().splitlines())
    out = "\n".join(line for line in lines if line)
    return out or None
