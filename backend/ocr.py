"""Local OCR for scanned pages, with bounded image size and serialized native calls."""
from threading import Lock

_lock=Lock()
_engine=None


def layout_rows(rows):
    """Group OCR boxes into lines and retain gaps between table columns."""
    lines = []
    for box, text, confidence in sorted(rows, key=lambda row: min(p[1] for p in row[0])):
        left = min(p[0] for p in box)
        top = min(p[1] for p in box)
        height = max(p[1] for p in box) - top
        center = top + height / 2
        line = next((line for line in lines if abs(line['center'] - center) <= max(3, min(line['height'], height) * .5)), None)
        if line is None:
            line = {'center': center, 'height': height, 'cells': []}
            lines.append(line)
        line['cells'].append((left, max(p[0] for p in box), str(text)))
    output = []
    for line in lines:
        cells = sorted(line['cells'])
        widths = [(right-left)/max(len(text), 1) for left, right, text in cells if text]
        char_width = max(1, sorted(widths)[len(widths)//2]) if widths else 8
        text = ''
        previous = None
        for left, right, value in cells:
            if previous is not None:
                text += ' ' * min(120, max(2, round((left-previous)/char_width)))
            text += value
            previous = right
        output.append(text)
    return '\n'.join(output)


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
                page=None
                bitmap=None
                try:
                    page=document[number-1]
                    width,height=page.get_size()
                    scale=min(2.5,2500/max(width,height))
                    bitmap=page.render(scale=scale)
                    rows,_=_engine(bitmap.to_numpy())
                    rows=rows or []
                    results[number]={'text':'\n'.join(str(row[1]) for row in rows),
                        'layout_text':layout_rows(rows),
                        'confidence':round(sum(float(row[2]) for row in rows)/len(rows),3) if rows else None}
                except Exception:
                    # A damaged image on one page must not discard the other
                    # pages' recognized fields or stop the rest of the PDF.
                    results[number] = {'error': 'OCR could not process this page.'}
                finally:
                    if bitmap is not None: bitmap.close()
                    if page is not None: page.close()
        finally:
            document.close()
    return results
