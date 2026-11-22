"""Child process for tenders.documents.parse: PDF bytes on stdin, one JSON object on stdout.

Never imported by the app. It reads untrusted bytes, so it runs as its own process
(``python -I pdftext.py <max_pages> <max_chars>``) that the parent kills after a time
budget and, on Linux, starts under a memory limit. Standard library and pypdf only: with
-I the app's packages aren't importable here anyway.

Output: {"pages": n, "pages_read": k, "text": "..."}, or {"error": "encrypted"}.
Only the first ``max_pages`` pages are read, and only whole pages while the text stays
within ``max_chars``: ``pages_read`` counts the pages whose text is all in "text", so
k < n means the document was read in part. A cut never splits a page, so it can't leave
half an email address or phone number for the redaction to miss; the one exception is a
first page that alone is over ``max_chars``, which is cut at the last whitespace before
the cap and not counted as read. Anything pypdf raises on a broken or hostile file is left
uncaught: the child exits non-zero, which the parent reports as parse_error, and its
stderr is never kept.
"""

import io
import json
import re
import sys


def cut_at_whitespace(text, limit):
    """At most ``limit`` characters, ending before the last whitespace in them ("" if none)."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    m = None
    for m in re.finditer(r"\s", head):
        pass
    return head[:m.start()] if m else ""


def main() -> int:
    max_pages, max_chars = int(sys.argv[1]), int(sys.argv[2])
    data = sys.stdin.buffer.read()
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        out = {"error": "encrypted"}
    else:
        n = len(reader.pages)
        parts, size, read = [], 0, 0
        for i in range(min(n, max_pages)):
            text = reader.pages[i].extract_text() or ""
            add = len(text) + (1 if parts else 0)  # the joining newline
            if size + add > max_chars:
                if not parts:
                    parts.append(cut_at_whitespace(text, max_chars))
                break
            parts.append(text)
            size += add
            read += 1
        out = {"pages": n, "pages_read": read, "text": "\n".join(parts)}
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
