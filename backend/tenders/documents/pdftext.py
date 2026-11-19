"""Child process for tenders.documents.parse: PDF bytes on stdin, one JSON object on stdout.

Never imported by the app. It reads untrusted bytes, so it runs as its own process
(``python -I pdftext.py <max_pages>``) that the parent kills after a time budget and, on
Linux, starts under a memory limit. Standard library and pypdf only: with -I the app's
packages aren't importable here anyway.

Output: {"pages": n, "text": "..."}, or {"error": "too_many_pages", "pages": n}, or
{"error": "encrypted"}. The page count is checked before any text is read. Anything pypdf
raises on a broken or hostile file is left uncaught: the child exits non-zero, which the
parent reports as parse_error, and its stderr is never kept.
"""

import io
import json
import sys


def main() -> int:
    max_pages = int(sys.argv[1])
    data = sys.stdin.buffer.read()
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        out = {"error": "encrypted"}
    elif (n := len(reader.pages)) > max_pages:
        out = {"error": "too_many_pages", "pages": n}
    else:
        out = {"pages": n,
               "text": "\n".join((page.extract_text() or "") for page in reader.pages)}
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
