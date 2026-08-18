import re
from html import unescape
from html.parser import HTMLParser


class _VisibleEmailTextExtractor(HTMLParser):
    _ignored_tags = {"head", "style", "script", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if self.ignored_depth:
            self.ignored_depth += 1
            return
        if tag.lower() in self._ignored_tags:
            self.ignored_depth = 1
            return
        attributes = {name.lower(): (value or "").lower() for name, value in attrs}
        if attributes.get("aria-hidden") == "true" or "display:none" in attributes.get("style", "").replace(" ", ""):
            self.ignored_depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self.ignored_depth:
            self.ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def visible_email_text(content: str, *, is_html: bool) -> str:
    """Return normalized text that is visible in an email body."""
    if not is_html:
        return re.sub(r"\s+", " ", unescape(content or "")).strip()

    extractor = _VisibleEmailTextExtractor()
    try:
        extractor.feed(content or "")
        extractor.close()
        text = "".join(extractor.parts)
    except Exception:
        text = re.sub(r"(?is)<(style|script|head|noscript|template|svg)\b.*?</\1>", " ", content or "")
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()
