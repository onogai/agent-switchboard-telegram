"""Safe Telegram message chunking for plain text and Telegram HTML."""
from __future__ import annotations

from html import escape
from html.parser import HTMLParser

from .store import DeliveryError

MAX_UNITS = 16384
PART_UNITS = 4096
MAX_HTML_SOURCE = 131072

# Telegram Bot API HTML tags (not arbitrary web HTML).
_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "blockquote", "span", "tg-spoiler", "tg-emoji",
}


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_text(text: str) -> list[str]:
    if not isinstance(text, str) or not text.strip() or units(text) > MAX_UNITS:
        raise DeliveryError("Message must be nonempty and at most 16384 UTF-16 units")
    parts = []
    current = []
    size = 0
    for char in text:
        width = units(char)
        if size + width > PART_UNITS:
            parts.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        parts.append("".join(current))
    return parts


class _TelegramHtmlSplitter(HTMLParser):
    """Split by rendered UTF-16 length, closing/reopening any spanning tags.

    Complete parsing happens before the first network call, so malformed markup
    cannot result in a partially delivered message.
    """

    def __init__(self, max_units: int):
        super().__init__(convert_charrefs=True)
        self.max_units = max_units
        self.total_units = 0
        self.part_units = 0
        self.parts: list[str] = []
        self.current: list[str] = []
        self.stack: list[tuple[str, str]] = []
        self.has_content = False

    def _flush(self):
        if not self.part_units:
            return
        self.parts.append("".join(self.current) +
                          "".join(f"</{tag}>" for tag, _ in reversed(self.stack)))
        self.current = [opener for _, opener in self.stack]
        self.part_units = 0

    def handle_starttag(self, tag, attrs):
        if tag not in _TAGS or (tag == "span" and attrs != [("class", "tg-spoiler")]):
            raise DeliveryError(f"Unsupported Telegram HTML tag: {tag}")
        opener = self.get_starttag_text()
        self.current.append(opener)
        self.stack.append((tag, opener))

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            raise DeliveryError("Unbalanced Telegram HTML tags")
        self.stack.pop()
        self.current.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        raise DeliveryError("Self-closing Telegram HTML tags are unsupported")

    def handle_comment(self, data):
        raise DeliveryError("HTML comments are unsupported")

    def handle_decl(self, decl):
        raise DeliveryError("HTML declarations are unsupported")

    def handle_pi(self, data):
        raise DeliveryError("HTML processing instructions are unsupported")

    def handle_data(self, data):
        if data.strip():
            self.has_content = True
        for char in data:
            width = 2 if ord(char) > 0xFFFF else 1
            if self.total_units + width > self.max_units:
                raise DeliveryError(f"Telegram HTML exceeds {self.max_units} rendered UTF-16 units")
            if self.part_units + width > PART_UNITS:
                self._flush()
            self.current.append(escape(char, quote=False))
            self.total_units += width
            self.part_units += width

    def finish(self) -> list[str]:
        self.close()
        if self.stack:
            raise DeliveryError("Unbalanced Telegram HTML tags")
        if not self.has_content:
            raise DeliveryError("Telegram HTML message must contain nonempty text")
        if self.part_units:
            self.parts.append("".join(self.current))
        return self.parts


def split_html(text: str, max_units: int = MAX_UNITS) -> list[str]:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_HTML_SOURCE:
        raise DeliveryError("Invalid or oversized Telegram HTML source")
    parser = _TelegramHtmlSplitter(max_units)
    parser.feed(text)
    return parser.finish()


def format_parts(text: str, parse_mode: str | None = None,
                 max_units: int = MAX_UNITS) -> list[str]:
    if parse_mode is None:
        return split_text(text)
    if parse_mode != "HTML":
        raise DeliveryError("parse_mode must be HTML or omitted")
    return split_html(text, max_units=max_units)
