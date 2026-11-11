"""Render assistant formatting as readable plain text at the transport boundary.

Keep this standalone module in sync with the Agent Telegram connector copy.
No user text is interpreted as Telegram HTML or Markdown.
"""
import re
from html.parser import HTMLParser


class _PlainHTML(HTMLParser):
    TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "span", "tg-spoiler"}
    BLOCKS = {"p", "div", "blockquote", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs).get("href", ""))
        elif tag == "br" or tag in self.BLOCKS:
            self.parts.append("\n" if tag != "li" else "\n• ")
        elif tag not in self.TAGS:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag == "a":
            href = self.links.pop() if self.links else ""
            if href.startswith(("https://", "http://", "tg://", "mailto:")):
                self.parts.append(f" ({href})")
        elif tag in self.BLOCKS:
            self.parts.append("\n")
        elif tag not in self.TAGS and tag != "br":
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(text: str) -> str:
    """Flatten formatting, preserving literal identifiers, arithmetic and code."""
    protected = []

    def protect(value):
        protected.append(value)
        return f"\x00TG{len(protected) - 1}\x00"

    # Preserve code and URLs before processing emphasis; '**' can be arithmetic.
    text = re.sub(r"(?m)^[ \t]*(`{3,}|~{3,})[^\n]*\n([\s\S]*?)^[ \t]*\1[ \t]*$",
                  lambda m: protect(m[2].rstrip("\n")), text)
    text = re.sub(r"(`+)([^`\n]+)\1", lambda m: protect(m[2]), text)
    # Parse only real supported HTML tags; comparisons such as 'x < 5' stay text.
    if re.search(r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|span|tg-spoiler|a|br|p|div|blockquote|li|ul|ol|h[1-6])(?:\s|/?>)", text, re.I):
        parser = _PlainHTML()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    text = re.sub(r"\[([^\]\n]+)\]\(([^\s]+(?:\s+[^)]*)?)\)",
                  lambda m: f"{m[1]} ({protect(m[2])})", text)
    def preserve_url(match):
        value = match[0]
        for delimiter in ("**", "__"):
            if text[:match.start()].endswith(delimiter) and value.endswith(delimiter):
                return protect(value[:-len(delimiter)]) + delimiter
        return protect(value)

    text = re.sub(r"(?:https?://|tg://|mailto:)[^\s<>]+", preserve_url, text)
    text = re.sub(r"(?<![\w*])(?:\*\*/[^\s]+|\*[^*\s]*\.[A-Za-z0-9*]+)", lambda m: protect(m[0]), text)
    text = re.sub(r"\\([*_`~\[\]])", lambda m: protect(m[1]), text)
    for delimiter in ("***", "___", "**", "__", "~~", "||", "*", "_"):
        escaped = re.escape(delimiter)
        char = re.escape(delimiter[0])
        pattern = rf"(?<![\w\\{char}]){escaped}(?!{char})(?=\S)(.+?)(?<=\S){escaped}(?![\w{char}])"
        for _ in range(3):
            updated = re.sub(pattern, lambda m: m[1], text, flags=re.S)
            if updated == text:
                break
            text = updated
    # Remove hanging emphasis from legacy cached chunks without touching IDs or glob paths.
    text = re.sub(r"(?<![\w\\])(?:\*\*|__|~~)(?=[^\s/*_~])", "", text)
    text = re.sub(r"(?<=[^\s/*_~])(?:\*\*|__|~~)(?!\w)", "", text)
    text = text.replace("`", "")  # Unclosed delimiters in older stored delivery chunks.
    text = re.sub(r"(?m)^ {0,3}#{1,6}[ \t]+", "", text)
    text = re.sub(r"(?m)^ {0,3}>[ \t]?", "", text)
    text = re.sub(r"(?m)^(\s*)[-*+][ \t]+", r"\1• ", text)
    text = re.sub(r"(?m)^\s*(?:\*{3,}|-{3,}|_{3,})\s*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # Placeholders may contain earlier placeholders (e.g. a URL in a Markdown link).
    for _ in range(3):
        restored = re.sub(r"\x00TG(\d+)\x00", lambda m: protected[int(m[1])], text)
        if restored == text:
            break
        text = restored
    return text
