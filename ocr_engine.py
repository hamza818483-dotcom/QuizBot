"""OCR Engine - Tesseract OCR with double pass for scanned PDFs"""
import io
from PIL import Image

def ocr_image(image_bytes: bytes, lang='eng+ben') -> str:
    """Extract text from image using Tesseract OCR with double pass"""
    try:
        import pytesseract
        img = Image.open(io.BytesIO(image_bytes))
        text1 = pytesseract.image_to_string(img, lang=lang)
        text2 = pytesseract.image_to_string(img, lang=lang, config='--psm 6')
        return text1 if len(text1) > len(text2) else text2
    except:
        return ""

def is_scanned_pdf(text: str) -> bool:
    """Check if extracted text is too short (scanned PDF)"""
    return len(text.strip()) < 50
