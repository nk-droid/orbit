"""A cover letter as a PDF, for a form that takes the letter as a file: its text set
in Helvetica on Letter-size pages, as a letter from a word processor looks.

The PDF is written here, by hand: Helvetica is one of the fonts every PDF reader
has, so nothing is embedded, and its text can be written in Windows-1252, which
holds the accented letters, dashes and quotes a letter uses. The checks
(checks.py) flag any character it cannot hold before the letter is used.
"""

import re
import textwrap
from pathlib import Path

from attachments import safe_name
from storage import data_dir

PAGE = (612, 792)
MARGIN = 72
SIZE = 11
LEADING = 15
# Characters to a line: Helvetica at 11 points averages under 5.6 points a
# character, and a line is 468 points wide.
LINE = 84
ENCODING = "cp1252"


def _escaped(line: str) -> bytes:
    text = line.encode(ENCODING, errors="replace")
    return text.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _lines(text: str) -> list[str]:
    lines: list[str] = []
    for paragraph in text.strip().splitlines():
        lines += textwrap.wrap(paragraph, LINE) if paragraph.strip() else [""]
    return lines


def pdf(text: str, title: str) -> bytes:
    """The letter as a PDF, titled `title`."""
    lines = _lines(text)
    per_page = (PAGE[1] - 2 * MARGIN) // LEADING
    pages = [lines[i : i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    count = len(pages)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{5 + 2 * i} 0 R".encode() for i in range(count))
        + f"] /Count {count} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
        b"<< /Title (" + _escaped(title) + b") /Producer (Orbit) >>",
    ]
    for page in pages:
        top = PAGE[1] - MARGIN - SIZE
        shown = b"\n".join(b"(" + _escaped(line) + b") Tj T*" for line in page)
        stream = (
            f"BT /F1 {SIZE} Tf {LEADING} TL {MARGIN} {top} Td\n".encode()
            + shown
            + b"\nET"
        )
        number = len(objects) + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE[0]} {PAGE[1]}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {number + 1} 0 R >>".encode()
        )
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    table = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 4 0 R >>\n"
        f"startxref\n{table}\n%%EOF\n"
    ).encode()
    return bytes(out)


def file_name(name: str) -> str:
    """What the letter's file is called, as the company sees it."""
    who = re.sub(r"\s+", " ", name).strip()
    return f"{who} - Cover letter.pdf" if who else "Cover letter.pdf"


def kept(text: str, name: str, application: int) -> Path:
    """The letter written to a PDF on the user's computer for one application, to be
    put into its form."""
    folder = data_dir() / "jobs" / "letters" / str(application)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / safe_name(file_name(name))
    path.write_bytes(pdf(text, file_name(name).removesuffix(".pdf")))
    return path
