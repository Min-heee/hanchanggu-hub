"""병원 안내 자료(마크다운)를 소제목 단위로 잘라 문의와 가까운 내용을 찾는다.

자료가 수백 건으로 늘면 임베딩 검색으로 바꾸면 되고, 나머지 코드는 search()만 쓰므로 그대로 둔다.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from .models import KnowledgeChunk

# 문의 내용과 상관없이 항상 함께 보내는 원칙 자료 (가격 정책, 응급 대응)
PINNED_SECTIONS = {"가격 안내 원칙", "응급 증상과 연락처"}
FOREIGN_SECTION = "외국인 환자 안내"


def bigrams(text: str) -> set[str]:
    compact = re.sub(r"[\W_]+", "", text.lower())
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


class KnowledgeBase:
    def __init__(self, chunks: list[KnowledgeChunk]):
        self.chunks = chunks
        self._grams = {c.id: bigrams(c.title + " " + c.text) for c in chunks}
        # '세요', '성형'처럼 어디에나 나오는 글자 조각은 가볍게, '부목'처럼 드문 조각은 무겁게 센다.
        df = Counter(g for grams in self._grams.values() for g in grams)
        self._idf = {g: math.log((len(chunks) + 1) / (n + 1)) + 1 for g, n in df.items()}
        self._unseen_idf = math.log(len(chunks) + 1) + 1

    @classmethod
    def load(cls, directory: Path) -> "KnowledgeBase":
        chunks: list[KnowledgeChunk] = []
        for path in sorted(directory.glob("*.md")):
            stem = path.stem.split("_", 1)[-1]
            doc_title, doc_category = path.stem, "공통"
            section: dict | None = None

            def flush() -> None:
                if section and section["lines"]:
                    chunks.append(
                        KnowledgeChunk(
                            id=f"{stem}-{len([c for c in chunks if c.id.startswith(stem + '-')]) + 1}",
                            title=f"{doc_title} · {section['title']}",
                            category=section["category"],
                            text=" ".join(section["lines"]),
                        )
                    )

            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("# "):
                    doc_title = line[2:].strip()
                elif line.startswith("## "):
                    flush()
                    section = {"title": line[3:].strip(), "category": doc_category, "lines": []}
                elif line.startswith("category:"):
                    value = line.split(":", 1)[1].strip()
                    if section is None:
                        doc_category = value
                    else:
                        section["category"] = value
                elif section is not None and line.strip() and not line.startswith(">"):
                    section["lines"].append(line.strip())
            flush()
        return cls(chunks)

    @staticmethod
    def section_of(chunk: KnowledgeChunk) -> str:
        return chunk.title.split(" · ")[-1]

    def get(self, chunk_id: str) -> KnowledgeChunk | None:
        return next((c for c in self.chunks if c.id == chunk_id), None)

    def by_section(self, section: str) -> KnowledgeChunk | None:
        return next((c for c in self.chunks if self.section_of(c) == section), None)

    def pinned(self) -> list[KnowledgeChunk]:
        return [c for c in self.chunks if self.section_of(c) in PINNED_SECTIONS]

    def search(self, query: str, k: int = 3, category: str | None = None) -> list[KnowledgeChunk]:
        q = bigrams(query)
        total = sum(self._idf.get(g, self._unseen_idf) for g in q) or 1.0
        scored = []
        for chunk in self.chunks:
            if self.section_of(chunk) in PINNED_SECTIONS:
                continue
            score = sum(self._idf[g] for g in q & self._grams[chunk.id]) / total
            if category and chunk.category == category:
                score += 0.1
            if score > 0.05:
                scored.append((score, chunk))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [chunk for _, chunk in scored[:k]]
