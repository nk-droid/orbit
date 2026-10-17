"""Writing with formatting: Markdown and HTML, turned into one clean kind of HTML.

Orbit may write an email, a document or an answer in Markdown or in HTML when it
needs headings, lists, a table or emphasis. Whatever it wrote, and whatever a
Markdown or HTML file holds, comes out of here as the same small set of tags and
nothing else: no scripts, no styles, no pictures fetched from somewhere.

That HTML is what the user is shown, and it is exactly what is then sent or
saved. So a draft cannot carry anything the user was not shown, and what somebody
else wrote, in a file or in a page Orbit read, cannot run in the app.
"""

import html
import re
from functools import cache
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

Format = Literal["plain", "markdown", "html"]

# What each format is called when talking about it.
NAMES = {"plain": "plain text", "markdown": "Markdown", "html": "HTML"}

# The tags kept, each with the attributes it may carry. Everything else is taken off
# and only its words stay.
_KEPT: dict[str, frozenset[str]] = {
    tag: frozenset()
    for tag in (
        *("p", "div", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6"),
        *("ul", "ol", "li", "dl", "dt", "dd", "blockquote", "pre"),
        *("table", "caption", "thead", "tbody", "tfoot", "tr", "th", "td"),
        *("a", "strong", "b", "em", "i", "u", "s", "del", "ins", "code"),
        *("sub", "sup", "mark", "small"),
    )
}
_KEPT["a"] = frozenset({"href"})
_KEPT["ol"] = frozenset({"start"})
_KEPT["th"] = _KEPT["td"] = frozenset({"colspan", "rowspan"})

# Tags that only group things. They become a plain block, so their lines stay apart.
_GROUPS = frozenset(
    {"section", "article", "header", "footer", "main", "nav", "aside", "figure"}
    | {"figcaption", "address", "details", "summary", "center"}
)
# Tags whose insides are not words to keep: code, styling, forms and embedded things.
_REMOVED = frozenset(
    {"script", "style", "head", "title", "template", "noscript", "iframe", "object"}
    | {"embed", "svg", "math", "canvas", "audio", "video", "map", "form", "button"}
    | {"select", "textarea"}
)
_VOID = frozenset({"br", "hr"})
# HTML lets a writer leave some tags open: a new list item ends the one before it.
# For each such tag, the open tags it ends, and the tag those cannot be looked for
# beyond: an item of an inner list does not end the item the list sits in.
_ENDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "li": (frozenset({"li"}), frozenset({"ul", "ol"})),
    "dt": (frozenset({"dt", "dd"}), frozenset({"dl"})),
    "dd": (frozenset({"dt", "dd"}), frozenset({"dl"})),
    "tr": (frozenset({"tr"}), frozenset({"table"})),
    "td": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "th": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "thead": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
    "tbody": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
    "tfoot": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
}
# Tags that cannot sit inside a paragraph, so starting one ends the paragraph.
_ENDS_PARAGRAPH = frozenset(
    {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "dl", "blockquote"}
    | {"pre", "table", "hr"}
)
# Tags that start a line of their own when the words alone are wanted.
_LINES = frozenset(
    {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt"}
    | {"dd", "blockquote", "pre", "table", "caption", "tr", "hr"}
)
# Tags with a blank line before and after them, as paragraphs have.
_PARAGRAPHS = frozenset(
    {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote", "pre", "table"}
)

_LINK_SCHEMES = frozenset({"http", "https", "mailto"})
_MAX_SPAN = 100
# "[x] " and "[ ] " at the start of a list item, as Markdown writes a ticked box.
_TICK_BOX = re.compile(r"(<li>(?:\s*<p>)?)\[([ xX])\] ")


def as_meant(text: str) -> str:
    """What the model meant to write, where it wrote line ends as the two characters \\n.

    Now and then a model escapes its line ends twice, and what arrives is one long
    line with a backslash and an n wherever a line should end. Left like that, a
    list or a table is not a list or a table at all. Text that has real line ends
    is taken as it is.
    """
    if "\n" in text or "\\n" not in text:
        return text
    return text.replace("\\r\\n", "\n").replace("\\n", "\n")


def _link(address: str) -> str | None:
    """The address if it leads to the web or to an email address, else None."""
    # Browsers ignore spaces and control characters inside a scheme. So must this.
    address = "".join(char for char in address if char > " ").strip()
    try:
        scheme = urlsplit(address).scheme.lower()
    except ValueError:
        return None
    return address if scheme in _LINK_SCHEMES else None


def _whole_number(value: str | None, upper: int) -> str | None:
    if value and value.strip().isdigit() and 0 < int(value) <= upper:
        return str(int(value))
    return None


class _Cleaner(HTMLParser):
    """Reads any HTML and writes it out again with only the kept tags.

    Nothing is copied through as it came. Every tag is written afresh from its
    name, and every word and address is escaped, so the result holds nothing that
    was not put there on purpose.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._open: list[str] = []
        # How deep inside removed tags the reader is. Their words are dropped too.
        self._removed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _REMOVED:
            self._removed += 1
            return
        if self._removed:
            return
        given = {name: value or "" for name, value in attrs}
        if tag == "img":
            # A picture would be fetched from wherever its address says. Its
            # caption stands in for it.
            if given.get("alt", "").strip():
                self.out.append(html.escape(f"[Picture: {given['alt'].strip()}]"))
            return
        if tag in _GROUPS:
            tag = "div"
        if tag not in _KEPT:
            return
        kept = self._attributes(tag, given)
        if tag == "a" and "href" not in kept:
            # A link that leads nowhere allowed is only its words.
            return
        self._end_what_it_ends(tag)
        written = "".join(
            f' {name}="{html.escape(value, quote=True)}"'
            for name, value in kept.items()
        )
        self.out.append(f"<{tag}{written}>")
        if tag not in _VOID:
            self._open.append(tag)

    def _end_what_it_ends(self, tag: str) -> None:
        """Closes the tags a browser would take as ended by this one starting."""
        if tag in _ENDS_PARAGRAPH and self._open and self._open[-1] == "p":
            self._close_through("p")
        if tag in _ENDS:
            ended, within = _ENDS[tag]
            last = None
            for opened in reversed(self._open):
                if opened in within:
                    break
                if opened in ended:
                    last = opened
            # The outermost of them: a new row ends the row and the cells in it.
            if last:
                self._close_through(last)

    def _close_through(self, tag: str) -> None:
        """Closes the innermost open `tag`, and whatever was left open inside it."""
        while self._open:
            opened = self._open.pop()
            self.out.append(f"</{opened}>")
            if opened == tag:
                return

    def _attributes(self, tag: str, given: dict[str, str]) -> dict[str, str]:
        kept: dict[str, str] = {}
        for name in sorted(_KEPT[tag] & given.keys()):
            if name == "href":
                value = _link(given[name])
            else:
                upper = 10**6 if name == "start" else _MAX_SPAN
                value = _whole_number(given[name], upper)
            if value is not None:
                kept[name] = value
        return kept

    def handle_endtag(self, tag: str) -> None:
        if tag in _REMOVED:
            self._removed = max(0, self._removed - 1)
            return
        if tag in _GROUPS:
            tag = "div"
        if not self._removed and tag in self._open:
            self._close_through(tag)

    def handle_data(self, data: str) -> None:
        if not self._removed:
            self.out.append(html.escape(data, quote=False))

    def finish(self) -> str:
        self.close()
        while self._open:
            self.out.append(f"</{self._open.pop()}>")
        return "".join(self.out).strip()


def clean(markup: str) -> str:
    """Any HTML as clean HTML: the kept tags, their words, and links to the web."""
    cleaner = _Cleaner()
    cleaner.feed(markup)
    return cleaner.finish()


@cache
def _markdown(breaks: bool) -> MarkdownIt:
    # HTML written inside Markdown is shown as the text it is, never used as HTML.
    options = {"html": False, "breaks": breaks}
    return MarkdownIt("commonmark", options).enable(["table", "strikethrough"])


def to_html(text: str, written_in: Format, *, breaks: bool = True) -> str | None:
    """What was written as clean HTML. None for plain text, which has no formatting.

    With `breaks`, the end of a line in Markdown is the end of a line, the way
    people write a letter. Without, lines run on into a paragraph, the way a
    Markdown file is read.
    """
    if written_in == "markdown":
        drawn = _markdown(breaks).render(text)
        boxed = _TICK_BOX.sub(
            lambda box: box[1] + ("☐ " if box[2] == " " else "☑ "), drawn
        )
        return clean(boxed)
    if written_in == "html":
        return clean(text)
    return None


class _Words(HTMLParser):
    """Reads clean HTML and keeps what a reader would read, line by line."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        # The break owed before the next word: 0 none, 1 a new line, 2 a blank line.
        self._break = 0
        self._space = False
        # A list marker was the last thing written: its words belong on the same line.
        self._marked = False
        # One entry for each list being read: None for bullets, else the next number.
        self._lists: list[int | None] = []
        self._link: str | None = None
        self._link_said: list[str] = []
        self._cells = 0
        self._pre = 0

    def _write(self, text: str) -> None:
        if self.out and self._break:
            self.out.append("\n" * self._break)
        elif self.out and self._space:
            self.out.append(" ")
        self._break, self._space, self._marked = 0, False, False
        self.out.append(text)

    def _new_line(self, tag: str) -> None:
        # Inside a list, items follow each other without the gaps paragraphs have.
        gap = 2 if tag in _PARAGRAPHS and not self._lists else 1
        if not self._marked:
            self._break = max(self._break, gap)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        given = dict(attrs)
        if tag in _LINES or tag == "br":
            self._new_line(tag)
        if tag == "ul":
            self._lists.append(None)
        elif tag == "ol":
            self._lists.append(int(given.get("start") or 1))
        elif tag == "li":
            number = self._lists[-1] if self._lists else None
            indent = "  " * max(0, len(self._lists) - 1)
            self._write(f"{indent}- " if number is None else f"{indent}{number}. ")
            self._marked = True
            if number is not None:
                self._lists[-1] = number + 1
        elif tag == "tr":
            self._cells = 0
        elif tag in ("td", "th"):
            if self._cells:
                self._space = False
                self._write(" | ")
            self._cells += 1
        elif tag == "a":
            self._link, self._link_said = given.get("href"), []
        elif tag == "pre":
            self._pre += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("ul", "ol") and self._lists:
            self._lists.pop()
        elif tag == "pre":
            self._pre = max(0, self._pre - 1)
        elif tag == "a" and self._link:
            address = self._link.removeprefix("mailto:")
            # Where a link leads is part of what is written, unless its words say it.
            if address not in "".join(self._link_said):
                self._space = False
                self._write(f" ({address})")
            self._link = None
        if tag in _LINES:
            self._new_line(tag)

    def handle_data(self, data: str) -> None:
        if self._pre:
            self._write(data.strip("\n"))
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip():
            self._space = True
            return
        if self._link is not None:
            self._link_said.append(text)
        # A list marker or a cell divider already ends in the space the word needs.
        glued = bool(self.out) and self.out[-1].endswith(" ")
        self._space = (self._space or text[0] == " ") and not glued
        self._write(text.strip())
        self._space = text[-1] == " "

    def finish(self) -> str:
        self.close()
        return "".join(self.out).strip()


def words(markup: str) -> str:
    """The words of clean HTML, as they would be read: a line each, lists marked.

    This is what Orbit reads out of a formatted draft, and what an email carries
    for mail programs that show no formatting.
    """
    reader = _Words()
    reader.feed(markup)
    return reader.finish()


def page(title: str, markup: str) -> str:
    """Clean HTML as a whole page, for a file of its own or the body of an email."""
    return (
        '<!doctype html>\n<html>\n<head>\n<meta charset="utf-8">\n'
        f"<title>{html.escape(title, quote=False)}</title>\n</head>\n"
        f"<body>\n{markup}\n</body>\n</html>\n"
    )
