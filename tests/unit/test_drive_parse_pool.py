import asyncio
import os
import time
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from app.workspace.drive_downloader import DOCX, PDF, PPTX, XLSX
from app.workspace.drive_parse_pool import ParsePool
from app.workspace.parsers import parse_file
from app.workspace.service import WorkspaceError


def delayed_file(mime, data):
    if data == b'kill':
        os._exit(1)
    if data == b'wait':
        time.sleep(60)
    if data == b'background-active':
        time.sleep(0.75)
    return parse_file(mime, data)


async def test_timeout_replaces_process_without_blocking_heartbeat():
    pool = ParsePool(processes=1, timeout=4, terminate_grace=0.1, parser=delayed_file)
    try:
        blocked = asyncio.create_task(pool.parse('text/plain', b'wait'))
        await asyncio.sleep(0.05)
        assert not blocked.done()
        with pytest.raises(WorkspaceError) as exc:
            await blocked
        assert exc.value.code == 'PARSE_TIMEOUT'
        assert (await pool.parse('text/plain', b'Van ban'))[0].content == 'Van ban'
    finally:
        await pool.close()


async def test_process_death_replaces_connection_and_accepts_next_file():
    pool = ParsePool(processes=1, timeout=5, terminate_grace=0.1, parser=delayed_file)
    try:
        with pytest.raises(WorkspaceError) as exc:
            await pool.parse('text/plain', b'kill')
        assert exc.value.code == 'PARSER_UNAVAILABLE'
        assert (await pool.parse('text/plain', b'van ban'))[0].content == 'van ban'
    finally:
        await pool.close()


async def test_waiting_queue_is_bounded_and_on_demand_work_has_priority():
    pool = ParsePool(processes=1, queue_size=2, timeout=8, parser=delayed_file)
    try:
        await pool.parse('text/plain', b'warm up')
        active = asyncio.create_task(pool.parse('text/plain', b'background-active'))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        low = asyncio.create_task(pool.parse('text/plain', b'background-waiting'))
        high = asyncio.create_task(pool.parse('text/plain', b'on-demand', priority=True))
        finished = []
        low.add_done_callback(lambda _: finished.append('low'))
        high.add_done_callback(lambda _: finished.append('high'))
        await asyncio.sleep(0)
        with pytest.raises(WorkspaceError) as exc:
            await pool.parse('text/plain', b'over-capacity')
        assert exc.value.code == 'PARSE_QUEUE_FULL'
        await asyncio.gather(active, low, high)
        assert finished == ['high', 'low']
    finally:
        await pool.close()


def test_uncompressed_office_limit_checked_before_loading():
    raw = BytesIO()
    with ZipFile(raw, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('document.xml', b'x' * (64 * 1024 * 1024 + 1))
    with pytest.raises(WorkspaceError) as exc:
        parse_file('application/vnd.openxmlformats-officedocument.wordprocessingml.document', raw.getvalue())
    assert exc.value.code == 'DECOMPRESSION_TOO_LARGE'


def test_markdown_source_and_vietnamese_size_limits():
    chunks = parse_file('text/markdown', ('# Quy trình\n' + 'thủ tục ' * 400 + '\n# Tài liệu\nHợp đồng').encode())
    assert {chunk.page_or_section for chunk in chunks} == {'Quy trình', 'Tài liệu'}
    assert all(len(chunk.content.encode()) <= 800 for chunk in chunks)
    assert chunks[-1].normalized == 'hop dong'


def test_docx_outputs_heading_and_table_content():
    from docx import Document

    document = Document()
    document.add_heading('Bảo hành', level=1)
    document.add_paragraph('Thời hạn cập nhật')
    document.add_table(rows=1, cols=1).cell(0, 0).text = '12 tháng'
    raw = BytesIO()
    document.save(raw)
    chunk, = parse_file(DOCX, raw.getvalue())
    assert chunk.page_or_section == 'Bảo hành'
    assert '12 tháng' in chunk.content


def test_xlsx_keeps_worksheet_names_and_cell_values():
    from openpyxl import Workbook

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = 'Tháng 1'
    worksheet.append(['Mặt hàng', 42])
    raw = BytesIO()
    workbook.save(raw)
    chunk, = parse_file(XLSX, raw.getvalue())
    assert chunk.page_or_section == 'Tháng 1'
    assert chunk.content == 'Mặt hàng\t42'


def test_pptx_uses_manifest_order_for_slide_citations():
    raw = BytesIO()
    with ZipFile(raw, 'w') as archive:
        archive.writestr('ppt/presentation.xml', '''
            <p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
                <p:sldIdLst><p:sldId r:id="second"/><p:sldId r:id="first"/></p:sldIdLst>
            </p:presentation>
        ''')
        archive.writestr('ppt/_rels/presentation.xml.rels', '''
            <Relationships>
                <Relationship Id="first" Type="schema/slide" Target="slides/slide1.xml"/>
                <Relationship Id="second" Type="schema/slide" Target="slides/slide2.xml"/>
            </Relationships>
        ''')
        for index, text in enumerate(('Thứ nhất', 'Thứ hai'), 1):
            archive.writestr(f'ppt/slides/slide{index}.xml', f'''
                <slide xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
                    <a:p><a:r><a:t>{text}</a:t></a:r></a:p>
                </slide>
            ''')
    chunks = parse_file(PPTX, raw.getvalue())
    assert [(part.page_or_section, part.content) for part in chunks] == [
        ('Slide 1', 'Thứ hai'), ('Slide 2', 'Thứ nhất'),
    ]


def test_pdf_text_keeps_page_citation():
    pypdf = pytest.importorskip('pypdf')
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()
    writer.add_blank_page(300, 300)
    page = writer.add_blank_page(300, 300)
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 10 Tf 12 24 Td (Contract signed) Tj ET')
    page[NameObject('/Contents')] = stream.flate_encode()
    page[NameObject('/Resources')] = DictionaryObject({
        NameObject('/Font'): DictionaryObject({
            NameObject('/F1'): DictionaryObject({
                NameObject('/Subtype'): NameObject('/Type1'),
                NameObject('/BaseFont'): NameObject('/Helvetica'),
            }),
        }),
    })
    raw = BytesIO()
    writer.write(raw)
    chunk, = parse_file(PDF, raw.getvalue())
    assert (chunk.page_or_section, chunk.content) == ('Page 2', 'Contract signed')
