"""OCR Utility for scanned PDFs"""
import io
from PIL import Image

def ocr_image(image_bytes: bytes, lang='eng+ben') -> str:
    """Extract text using Tesseract OCR with double pass"""
    try:
        import pytesseract
        img = Image.open(io.BytesIO(image_bytes))
        text1 = pytesseract.image_to_string(img, lang=lang)
        text2 = pytesseract.image_to_string(img, lang=lang, config='--psm 6')
        return text1 if len(text1) > len(text2) else text2
    except:
        return ""

def ocr_pdf_page(pdf_path: str, page_num: int) -> str:
    """OCR a specific PDF page"""
    try:
        from pdf2image import convert_from_path
        images = convert_from_path(pdf_path, first_page=page_num, last_page=page_num)
        if images:
            return ocr_image(images[0].tobytes())
    except:
        pass
    return ""
