"""Extract plain text from a .docx file using only stdlib.

Usage:
  python pipeline/docx_to_text.py "path/to/file.docx"
  python pipeline/docx_to_text.py "path/to/file.docx" "output.md"
"""
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_to_text(path):
    out = []
    with zipfile.ZipFile(path) as z:
        with z.open("word/document.xml") as f:
            tree = ET.parse(f)
    body = tree.getroot().find(f"{W_NS}body")
    if body is None:
        return ""
    for p in body.iter(f"{W_NS}p"):
        # heading style?
        style_id = None
        pPr = p.find(f"{W_NS}pPr")
        if pPr is not None:
            pStyle = pPr.find(f"{W_NS}pStyle")
            if pStyle is not None:
                style_id = pStyle.get(f"{W_NS}val")
        text = "".join(t.text or "" for t in p.iter(f"{W_NS}t"))
        if style_id and style_id.lower().startswith("heading"):
            level = re.search(r"\d", style_id)
            level = int(level.group(0)) if level else 1
            out.append("#" * level + " " + text)
        elif style_id and style_id.lower() == "title":
            out.append("# " + text)
        else:
            out.append(text)
        out.append("")
    return "\n".join(out)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: docx_to_text.py <input.docx> [output.md]")
    src = Path(sys.argv[1])
    if not src.exists():
        sys.exit(f"not found: {src}")
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".md")
    txt = docx_to_text(src)
    out_path.write_text(txt, encoding="utf-8")
    print(f"wrote {len(txt):,} chars -> {out_path}")


if __name__ == "__main__":
    main()
