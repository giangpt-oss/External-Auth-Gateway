import logging
from io import BytesIO


def sections(data: bytes, max_bytes: int):
    from pypdf import PdfReader, apply_configuration
    from pypdf.errors import LimitReachedError

    from app.workspace.service import WorkspaceError

    # Provider documents can contain sensitive object names in parser diagnostics.
    logging.getLogger("pypdf").setLevel(logging.CRITICAL + 1)
    with apply_configuration(
        maximum_declared_stream_length=max_bytes,
        array_based_stream_maximum_output_length=max_bytes,
        jbig2_maximum_output_length=max_bytes,
        lzw_maximum_output_length=max_bytes,
        run_length_maximum_output_length=max_bytes,
        zlib_maximum_output_length=max_bytes,
        jbig2dec_binary=None,
    ):
        try:
            reader = PdfReader(BytesIO(data))
            decoded_bytes = 0
            for number, page in enumerate(reader.pages, 1):
                contents = page.get_contents()
                if contents is not None:
                    decoded_bytes += len(contents.get_data())
                    if decoded_bytes > max_bytes:
                        raise WorkspaceError("DECOMPRESSION_TOO_LARGE", "PDF exceeds 64 MiB decoded.")
                yield f"Page {number}", page.extract_text()
        except LimitReachedError as exc:
            raise WorkspaceError("DECOMPRESSION_TOO_LARGE", "PDF exceeds decoding limits.") from exc
