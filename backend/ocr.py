"""Local OCR for scanned pages, with bounded image size and serialized native calls."""
from threading import Lock

_lock=Lock()
_engine=None


def read_scanned_pages(content, page_numbers):
    global _engine
    import pypdfium2 as pdfium
    from rapidocr_onnxruntime import RapidOCR
    results={}
    with _lock:
        if _engine is None:
            _engine=RapidOCR(intra_op_num_threads=2,inter_op_num_threads=1)
        document=pdfium.PdfDocument(content)
        try:
            for number in page_numbers:
                page=document[number-1]
                bitmap=None
                try:
                    width,height=page.get_size()
                    scale=min(2.5,2500/max(width,height))
                    bitmap=page.render(scale=scale)
                    rows,_=_engine(bitmap.to_numpy())
                    rows=rows or []
                    results[number]={'text':'\n'.join(str(row[1]) for row in rows),
                        'confidence':round(sum(float(row[2]) for row in rows)/len(rows),3) if rows else None}
                finally:
                    if bitmap is not None: bitmap.close()
                    page.close()
        finally:
            document.close()
    return results
