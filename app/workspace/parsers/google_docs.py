from io import BytesIO

from docx import Document
from docx.table import Table


def sections(data: bytes):
    document = Document(BytesIO(data))
    heading, content = "Document", []
    for item in document.iter_inner_content():
        if isinstance(item, Table):
            content.extend("\t".join(cell.text for cell in row.cells) for row in item.rows)
        elif item.style.name.startswith(("Heading", "Title")) and item.text.strip():
            yield heading, "\n".join(content)
            heading, content = item.text.strip(), [item.text]
        elif item.text.strip():
            content.append(item.text)
    yield heading, "\n".join(content)
