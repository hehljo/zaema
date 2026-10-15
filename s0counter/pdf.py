"""Minimaler PDF-Schreiber für den Jahresbericht.

Erzeugt PDF 1.4 mit den 14 Standardschriften, die jeder Betrachter kennt —
dadurch sind keine Schriftdateien einzubetten und keine Fremdpakete zu
installieren. Auf einem Pi, der jahrelang unbeaufsichtigt läuft, ist jede
nicht installierte Abhängigkeit eine Fehlerquelle weniger.

Unterstützt wird, was ein Abrechnungsbeleg braucht: Text in drei Schnitten,
Linien, Flächen und Tabellen. Keine Bilder, keine Transparenz, keine Bögen.
"""
import zlib
from dataclasses import dataclass, field

# DIN A4 in Punkt, 72 dpi
PAGE_WIDTH = 595.28
PAGE_HEIGHT = 841.89

FONTS = {
    'regular': 'Helvetica',
    'bold': 'Helvetica-Bold',
    'mono': 'Courier',
    'mono-bold': 'Courier-Bold',
}

# Zeichenbreiten der Standardschriften, in 1/1000 em. Für Helvetica sind
# die Breiten proportional, für Courier fest bei 600.
_HELVETICA_WIDTHS = {
    ' ': 278, '!': 278, '"': 355, '#': 556, '$': 556, '%': 889, '&': 667,
    "'": 191, '(': 333, ')': 333, '*': 389, '+': 584, ',': 278, '-': 333,
    '.': 278, '/': 278, '0': 556, '1': 556, '2': 556, '3': 556, '4': 556,
    '5': 556, '6': 556, '7': 556, '8': 556, '9': 556, ':': 278, ';': 278,
    '<': 584, '=': 584, '>': 584, '?': 556, '@': 1015, 'A': 667, 'B': 667,
    'C': 722, 'D': 722, 'E': 667, 'F': 611, 'G': 778, 'H': 722, 'I': 278,
    'J': 500, 'K': 667, 'L': 556, 'M': 833, 'N': 722, 'O': 778, 'P': 667,
    'Q': 778, 'R': 722, 'S': 667, 'T': 611, 'U': 722, 'V': 667, 'W': 944,
    'X': 667, 'Y': 667, 'Z': 611, '[': 278, '\\': 278, ']': 278, '^': 469,
    '_': 556, '`': 333, 'a': 556, 'b': 556, 'c': 500, 'd': 556, 'e': 556,
    'f': 278, 'g': 556, 'h': 556, 'i': 222, 'j': 222, 'k': 500, 'l': 222,
    'm': 833, 'n': 556, 'o': 556, 'p': 556, 'q': 556, 'r': 333, 's': 500,
    't': 278, 'u': 556, 'v': 500, 'w': 722, 'x': 500, 'y': 500, 'z': 500,
    '{': 334, '|': 260, '}': 334, '~': 584,
    'ä': 556, 'ö': 556, 'ü': 556, 'Ä': 667, 'Ö': 778, 'Ü': 722, 'ß': 556,
    '€': 556, '°': 400, '·': 278, '–': 556, '—': 1000, '„': 333, '“': 333,
}


def text_width(text: str, size: float, font: str = 'regular') -> float:
    """Breite eines Textes in Punkt."""
    if font.startswith('mono'):
        return len(text) * size * 0.6
    total = sum(_HELVETICA_WIDTHS.get(c, 556) for c in text)
    factor = 1.0 if font == 'regular' else 1.06   # Bold ist etwas breiter
    return total * size * factor / 1000.0


# Zeichen, die WinAnsiEncoding kennt, Latin-1 aber nicht. Ohne diese
# Zuordnung würden Gedankenstrich und typografische Anführungszeichen als
# Fragezeichen erscheinen.
_WINANSI = {
    '€': 0x80, '‚': 0x82, '„': 0x84, '…': 0x85, '†': 0x86, '‡': 0x87,
    '‰': 0x89, '‹': 0x8B, '‘': 0x91, '’': 0x92, '“': 0x93, '”': 0x94,
    '•': 0x95, '–': 0x96, '—': 0x97, '™': 0x99, '›': 0x9B,
}


def _escape(text: str) -> bytes:
    """Text nach WinAnsiEncoding, wie in den Font-Objekten deklariert."""
    out = bytearray()
    for char in text:
        byte = _WINANSI.get(char)
        if byte is None:
            try:
                byte = char.encode('latin-1')[0]
            except UnicodeEncodeError:
                byte = ord('?')
        if byte in (0x28, 0x29, 0x5C):    # ( ) \
            out.append(0x5C)
        out.append(byte)
    return bytes(out)


@dataclass
class Page:
    content: list = field(default_factory=list)

    def _add(self, line: str) -> None:
        self.content.append(line)

    def text(self, x: float, y: float, value: str, size: float = 10,
             font: str = 'regular', gray: float = 0.0) -> None:
        """Text an absoluter Position. y wird von oben gemessen."""
        self._add(f'BT /F{_font_index(font)} {size:.2f} Tf '
                  f'{gray:.3f} g '
                  f'{x:.2f} {PAGE_HEIGHT - y:.2f} Td '
                  f'({_escape(value).decode("latin-1")}) Tj ET')

    def text_right(self, x: float, y: float, value: str, size: float = 10,
                   font: str = 'regular', gray: float = 0.0) -> None:
        """Rechtsbündig an x — für Zahlenspalten."""
        self.text(x - text_width(value, size, font), y, value, size, font, gray)

    def line(self, x1: float, y1: float, x2: float, y2: float,
             width: float = 0.5, gray: float = 0.0) -> None:
        self._add(f'{gray:.3f} G {width:.2f} w '
                  f'{x1:.2f} {PAGE_HEIGHT - y1:.2f} m '
                  f'{x2:.2f} {PAGE_HEIGHT - y2:.2f} l S')

    def rect(self, x: float, y: float, width: float, height: float,
             gray: float = 0.9, stroke: bool = False) -> None:
        operator = 'B' if stroke else 'f'
        self._add(f'{gray:.3f} g 0.000 G 0.5 w '
                  f'{x:.2f} {PAGE_HEIGHT - y - height:.2f} '
                  f'{width:.2f} {height:.2f} re {operator}')

    def stream(self) -> bytes:
        return '\n'.join(self.content).encode('latin-1')


def _font_index(name: str) -> int:
    return list(FONTS).index(name) + 1


class Document:
    """Sammelt Seiten und schreibt die PDF-Datei."""

    def __init__(self, title: str = '', author: str = ''):
        self.pages: list[Page] = []
        self.title = title
        self.author = author

    def new_page(self) -> Page:
        page = Page()
        self.pages.append(page)
        return page

    def save(self, path: str) -> None:
        with open(path, 'wb') as handle:
            handle.write(self.build())

    def build(self) -> bytes:
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        font_ids = {}
        for key, base in FONTS.items():
            font_ids[key] = add(
                f'<< /Type /Font /Subtype /Type1 /BaseFont /{base} '
                f'/Encoding /WinAnsiEncoding >>'.encode('latin-1'))

        font_entries = ' '.join(
            f'/F{_font_index(key)} {font_ids[key]} 0 R' for key in FONTS)

        # Der Seitenbaum wird von jeder Seite referenziert, kennt aber selbst
        # erst am Ende alle Seiten. Die Nummer wird deshalb reserviert und
        # der Inhalt nachgetragen — eine vorausberechnete Nummer würde bei
        # jeder Änderung an der Objektreihenfolge still falsch.
        pages_id = add(b'')
        page_ids = []

        for page in self.pages:
            raw = page.stream()
            compressed = zlib.compress(raw)
            stream_id = add(
                b'<< /Length ' + str(len(compressed)).encode() +
                b' /Filter /FlateDecode >>\nstream\n' + compressed +
                b'\nendstream')
            page_ids.append(add(
                f'<< /Type /Page /Parent {pages_id} 0 R '
                f'/MediaBox [0 0 {PAGE_WIDTH:.2f} {PAGE_HEIGHT:.2f}] '
                f'/Resources << /Font << {font_entries} >> >> '
                f'/Contents {stream_id} 0 R >>'.encode('latin-1')))

        kids = ' '.join(f'{i} 0 R' for i in page_ids)
        objects[pages_id - 1] = (
            f'<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>'
            .encode('latin-1'))

        info_id = add(
            b'<< /Title (' + _escape(self.title) +
            b') /Author (' + _escape(self.author) +
            b') /Producer (s0counter) >>')
        catalog_id = add(f'<< /Type /Catalog /Pages {pages_id} 0 R >>'
                         .encode('latin-1'))

        # Datei zusammensetzen
        out = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
        offsets = [0]
        for index, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f'{index} 0 obj\n'.encode() + body + b'\nendobj\n'

        xref_position = len(out)
        out += f'xref\n0 {len(objects) + 1}\n'.encode()
        out += b'0000000000 65535 f \n'
        for offset in offsets[1:]:
            out += f'{offset:010d} 00000 n \n'.encode()
        out += (f'trailer\n<< /Size {len(objects) + 1} '
                f'/Root {catalog_id} 0 R /Info {info_id} 0 R >>\n'
                f'startxref\n{xref_position}\n%%EOF\n').encode()

        return bytes(out)
