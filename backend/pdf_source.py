"""Read source pages from PDF bytes independently of model-generated fields."""
from io import BytesIO
from contextlib import closing
from threading import Lock

from pypdf import PdfReader
import pypdfium2 as pdfium

VERSION = "python-pdf-source-v1"
_render_lock = Lock()  # PDFium is not thread-safe.


def read_source(content: bytes):
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("The source PDF requires a password")
        count = len(reader.pages)
        if not 1 <= count <= 250:
            raise ValueError("Source PDF must contain between 1 and 250 pages")
    except Exception as exc:
        raise ValueError("The stored PDF could not be read by Python. Open the original PDF to review it") from exc
    pages, warnings = [], []
    for number, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text(extraction_mode="layout") or "").strip()
        except Exception:
            text = ""
            warnings.append(f"Page {number}: text could not be read; review the original page image.")
        pages.append({"page": number, "text": text, "extraction_method": "python-pypdf"})
    return {"reader_version": VERSION, "provider": "python", "pages": pages,
            "warnings": warnings}


def render_source_page(content: bytes, page_number: int):
    with _render_lock:
        try:
            with closing(pdfium.PdfDocument(content)) as document:
                if not 1 <= page_number <= len(document):
                    raise IndexError("Source page does not exist")
                with closing(document[page_number - 1]) as page:
                    width, height = page.get_size()
                    scale = min(1.5, 2000 / max(width, height))
                    with closing(page.render(scale=scale)) as bitmap:
                        with bitmap.to_pil() as picture:
                            output = BytesIO()
                            picture.save(output, format="PNG")
                            return output.getvalue()
        except IndexError:
            raise
        except Exception as exc:
            raise ValueError("The source PDF page could not be rendered by Python") from exc
