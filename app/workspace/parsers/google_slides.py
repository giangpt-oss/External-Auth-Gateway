from io import BytesIO
from posixpath import join, normpath
from zipfile import ZipFile

from lxml import etree

PRESENTATION = "http://schemas.openxmlformats.org/presentationml/2006/main"
RELATIONS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"


def sections(data: bytes):
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    with ZipFile(BytesIO(data)) as archive:
        rels = etree.fromstring(archive.read("ppt/_rels/presentation.xml.rels"), parser)
        paths = {
            row.attrib["Id"]: row.attrib["Target"] for row in rels
            if row.get("Type", "").endswith("/slide") and row.get("TargetMode") != "External"
        }
        manifest = etree.fromstring(archive.read("ppt/presentation.xml"), parser)
        for number, slide in enumerate(manifest.iter(f"{{{PRESENTATION}}}sldId"), 1):
            target = paths[slide.attrib[f"{{{RELATIONS}}}id"]]
            path = normpath(target.lstrip("/") if target.startswith("/") else join("ppt", target))
            if not path.startswith("ppt/slides/"):
                raise ValueError("Invalid PowerPoint slide path.")
            root = etree.fromstring(archive.read(path), parser)
            paragraphs = (
                "".join(node.text or "" for node in paragraph.iter(f"{{{DRAWING}}}t"))
                for paragraph in root.iter(f"{{{DRAWING}}}p")
            )
            yield f"Slide {number}", "\n".join(paragraphs)
