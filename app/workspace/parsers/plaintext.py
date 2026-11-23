import csv
import re
from io import StringIO

HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


def text(data: bytes):
    yield "Document", data.decode("utf-8-sig")


def markdown(data: bytes):
    heading, content = "Document", []
    for line in data.decode("utf-8-sig").splitlines():
        match = HEADING.fullmatch(line)
        if match:
            yield heading, "\n".join(content)
            heading, content = match[1], []
        else:
            content.append(line)
    yield heading, "\n".join(content)


def table(data: bytes):
    csv.field_size_limit(len(data) or 1)
    with StringIO(data.decode("utf-8-sig"), newline="") as source:
        yield "CSV", "\n".join("\t".join(row) for row in csv.reader(source))
