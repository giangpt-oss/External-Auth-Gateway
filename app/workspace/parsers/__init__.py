"""Extract source-labeled text from supported Drive media in the parser process."""
from dataclasses import dataclass, field
from functools import partial
from io import BytesIO
from zipfile import ZipFile

from app.workspace.drive_downloader import DOCX, PDF, PPTX, XLSX
from app.workspace.parsers import google_docs, google_sheets, google_slides, pdf_parser, plaintext
from app.workspace.service import MAX_MEDIA_BYTES, WorkspaceError
from app.workspace.vn_normalizer import normalize_vietnamese

MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_CHUNK_BYTES = 800
OVERLAP_BYTES = 80
PARSER_VERSION = "1"
OFFICE_MIMES = {DOCX, XLSX, PPTX}
PARSERS = {
    DOCX: google_docs.sections,
    XLSX: partial(google_sheets.sections, max_bytes=MAX_DECOMPRESSED_BYTES),
    PPTX: google_slides.sections,
    PDF: partial(pdf_parser.sections, max_bytes=MAX_DECOMPRESSED_BYTES),
    "text/plain": plaintext.text,
    "text/markdown": plaintext.markdown,
    "text/csv": plaintext.table,
}


@dataclass(frozen=True)
class ParsedChunk:
    content: str = field(repr=False)
    page_or_section: str
    normalized: str = field(repr=False)


def _check_archive(data: bytes) -> None:
    with ZipFile(BytesIO(data)) as archive:
        if sum(member.file_size for member in archive.infolist()) > MAX_DECOMPRESSED_BYTES:
            raise WorkspaceError("DECOMPRESSION_TOO_LARGE", "Office file exceeds 64 MiB uncompressed.")


def _chunks(text: str):
    # UTF-8 bytes upper-bound the corresponding byte-level BPE token count.
    # Adjust the byte boundaries so they never split a code point.
    data = text.encode("utf-8")
    start = 0
    while start < len(data):
        stop = min(len(data), start + MAX_CHUNK_BYTES)
        if stop < len(data):
            while data[stop] & 0xC0 == 0x80:
                stop -= 1
            boundary = max(data.rfind(b" ", start + MAX_CHUNK_BYTES // 2, stop),
                           data.rfind(b"\n", start + MAX_CHUNK_BYTES // 2, stop))
            if boundary >= 0:
                stop = boundary + 1
        value = data[start:stop].decode("utf-8").strip()
        if value:
            yield value
        if stop == len(data):
            break
        start = stop - OVERLAP_BYTES
        while data[start] & 0xC0 == 0x80:
            start += 1
        word_start = data.find(b" ", start, min(stop, start + OVERLAP_BYTES // 2))
        if word_start >= 0:
            start = word_start + 1


def parse_file(mime: str, data: bytes) -> list[ParsedChunk]:
    if len(data) > MAX_MEDIA_BYTES:
        raise WorkspaceError("RESPONSE_TOO_LARGE", "Drive file exceeds 8 MiB.")
    parser = PARSERS.get(mime)
    if parser is None:
        raise WorkspaceError("UNSUPPORTED_FILE", "No parser for this Drive file format.")
    if not data:
        return []
    if mime in OFFICE_MIMES:
        _check_archive(data)
    result, size = [], 0
    for heading, text in parser(data):
        size += len(text.encode("utf-8"))
        if size > MAX_DECOMPRESSED_BYTES:
            raise WorkspaceError("DECOMPRESSION_TOO_LARGE", "Drive text exceeds 64 MiB.")
        result.extend(ParsedChunk(chunk, heading[:128], normalize_vietnamese(chunk))
                      for chunk in _chunks(text))
    return result
