"""Split SOP markdown into retrievable chunks, one per `##` section.

Embedding and pgvector ingest need the Gemini key and are added with the retriever.
"""

import re
from pathlib import Path

from pydantic import BaseModel

SOP_DIR = Path(__file__).parent / "sops"


class Chunk(BaseModel):
    id: str  # "settlement#kyc-re-verification-hold"
    source: str  # file name
    title: str  # "Settlements > KYC re-verification hold"
    text: str


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def chunk_markdown(source: str, markdown: str) -> list[Chunk]:
    doc_title, chunks = source, []
    section, lines = None, []

    def flush() -> None:
        body = "\n".join(lines).strip()
        if section and body:
            chunks.append(
                Chunk(
                    id=f"{Path(source).stem}#{_slug(section)}",
                    source=source,
                    title=f"{doc_title} > {section}",
                    text=body,
                )
            )

    for line in markdown.splitlines():
        if line.startswith("# "):
            doc_title = line[2:].strip()
        elif line.startswith("## "):
            flush()
            section, lines = line[3:].strip(), []
        else:
            lines.append(line)
    flush()
    return chunks


def load_sops(directory: Path = SOP_DIR) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(directory.glob("*.md")):
        if path.name != "README.md":
            chunks += chunk_markdown(path.name, path.read_text())
    return chunks
