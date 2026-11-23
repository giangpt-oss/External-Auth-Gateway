from io import BytesIO

from openpyxl import load_workbook


def sections(data: bytes, max_bytes: int):
    from app.workspace.service import WorkspaceError

    workbook = load_workbook(BytesIO(data), read_only=True, data_only=True, keep_links=False)
    size = 0
    try:
        for sheet in workbook.worksheets:
            rows = []
            for row in sheet.iter_rows(values_only=True):
                cells = []
                for cell in row:
                    value = str(cell) if cell is not None else ""
                    size += 1 + len(value.encode("utf-8"))
                    if size > max_bytes:
                        raise WorkspaceError("DECOMPRESSION_TOO_LARGE", "Drive text exceeds 64 MiB.")
                    cells.append(value)
                line = "\t".join(cells).rstrip()
                if line:
                    rows.append(line)
            yield sheet.title, "\n".join(rows)
    finally:
        workbook.close()
