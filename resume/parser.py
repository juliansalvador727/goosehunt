"""Extract plain text from a resume PDF using pdfplumber."""

from pathlib import Path

import pdfplumber


def extract_text(pdf_path: Path) -> str:
    """Return all text from the PDF, pages joined by newline."""
    with pdfplumber.open(pdf_path) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    return "\n".join(pages).strip()


if __name__ == "__main__":
    import sys
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent / "resume.pdf"
    print(extract_text(path))
