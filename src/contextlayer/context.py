"""Context assembly: from a question to the exact text the model will read.

This is the "context layer" in one place. Given a question, it:

1. retrieves chunks (vector, full-text or hybrid),
2. drops weak matches with a relevance threshold,
3. optionally follows document links for multi-hop questions,
4. recalls the agent's memories, by similarity and through the graph,
5. packs everything into a token budget, de-duplicating parent sections,
6. renders it with numbered sources the model can cite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape
from typing import TYPE_CHECKING, Literal

from contextlayer.chunking import count_tokens
from contextlayer.memory import Memory, MemoryStore
from contextlayer.retrieval import Hit, Retriever

if TYPE_CHECKING:
    from contextlayer.llm import LLM


@dataclass
class Source:
    index: int
    document_id: str
    title: str
    text: str
    hops: int = 0


@dataclass
class ContextPacket:
    question: str
    sources: list[Source]
    memories: list[Memory]
    tokens: int
    dropped: list[Hit] = field(default_factory=list)  # retrieved but cut by threshold or budget
    dropped_memories: list[Memory] = field(default_factory=list)  # recalled but over the memory budget

    def render(self) -> str:
        # Only attribute values are escaped. Body text goes in as written: the tags
        # are labels for the model, not a security boundary, and escaping would turn
        # `--group <group>` into `--group &lt;group&gt;` in commands the model repeats.
        parts = []
        if self.memories:
            lines = "\n".join(f"- ({m.kind}) {m.content}" for m in self.memories)
            parts.append(f"<memories>\n{lines}\n</memories>")
        if self.sources:
            docs = "\n".join(
                f'<source index="{s.index}" id="{s.document_id}" title="{escape(s.title)}">\n'
                f"{s.text}\n</source>"
                for s in self.sources
            )
            parts.append(f"<sources>\n{docs}\n</sources>")
        else:
            parts.append("<sources>No relevant sources were found.</sources>")
        return "\n\n".join(parts)


class ContextBuilder:
    def __init__(
        self,
        retriever: Retriever,
        memory: MemoryStore | None = None,
        *,
        mode: Literal["vector", "fulltext", "hybrid"] = "hybrid",
        k: int = 6,
        max_distance: float | None = 0.45,
        min_bm25: float = 3.0,
        expand_graph: bool = True,
        graph_depth: int = 2,
        memories: int = 4,
        token_budget: int = 3000,
        memory_share: float = 0.25,
    ):
        """`token_budget` covers memories and sources together; memories may use at
        most `memory_share` of it, so a few long memories can't crowd out the sources."""
        self.retriever = retriever
        self.memory = memory
        self.mode = mode
        self.k = k
        self.max_distance = max_distance
        self.min_bm25 = min_bm25
        self.expand_graph = expand_graph
        self.graph_depth = graph_depth
        self.memories = memories
        self.token_budget = token_budget
        self.memory_share = memory_share

    def _relevant(self, hit: Hit) -> bool:
        """Keep a hit if either retriever is confident about it."""
        if self.max_distance is None:
            return True
        distance, bm25 = hit.signals.get("distance"), hit.signals.get("bm25")
        if distance is not None and distance <= self.max_distance:
            return True
        return bm25 is not None and bm25 >= self.min_bm25

    def retrieve(self, question: str) -> tuple[list[Hit], list[Hit]]:
        """(kept, dropped) hits for the question."""
        search = getattr(self.retriever, self.mode)
        hits = search(question, self.k)
        kept = [h for h in hits if self._relevant(h)]
        dropped = [h for h in hits if not self._relevant(h)]
        if self.expand_graph and kept:
            kept += self.retriever.expand(question, kept, depth=self.graph_depth)
        return kept, dropped

    def build(self, question: str) -> ContextPacket:
        hits, dropped = self.retrieve(question)

        recalled: list[Memory] = []
        if self.memory:
            # Building context is not evidence a memory was useful, so don't reinforce here.
            recalled = self.memory.recall(question, k=self.memories, reinforce=False)
            known = {m.id for m in recalled}
            documents = list(dict.fromkeys(h.document_id for h in hits))
            recalled += [m for m in self.memory.about(documents) if m.id not in known]

        # Memories first, best first, within their share of the budget.
        memories, dropped_memories, used = [], [], 0
        for m in recalled:
            cost = count_tokens(m.content)
            if used + cost > self.token_budget * self.memory_share:
                dropped_memories.append(m)
                continue
            memories.append(m)
            used += cost

        sources: dict[str, Source] = {}  # one source per document, chunks appended in rank order
        seen_text: set[str] = set()
        for hit in hits:
            text = hit.generation_text
            if text in seen_text:  # two children of one parent: send the parent once
                continue
            cost = count_tokens(text)
            if used + cost > self.token_budget and text != hit.text:
                text, cost = hit.text, count_tokens(hit.text)  # parent too big: fall back to the child
            if used + cost > self.token_budget:
                dropped.append(hit)
                continue
            seen_text.add(text)
            used += cost
            if hit.document_id in sources:
                sources[hit.document_id].text += "\n\n" + text
            else:
                sources[hit.document_id] = Source(
                    index=len(sources) + 1, document_id=hit.document_id, title=hit.title,
                    text=text, hops=int(hit.signals.get("hops", 0)),
                )
        return ContextPacket(question, list(sources.values()), memories, used, dropped, dropped_memories)


ANSWER_SYSTEM = """You answer questions for engineers at Lumen Pay using the sources and memories provided.

- Use only the information in <sources> and <memories>. If they don't contain the answer, say so plainly.
- Cite sources inline with their index in square brackets, like [2]. Cite every claim that comes from a source.
- Memories describe the person you are talking to; use them to tailor the answer, not as evidence."""


@dataclass
class Answer:
    text: str
    context: ContextPacket

    @property
    def cited(self) -> list[Source]:
        """Sources the answer cites, written as [2], [1, 3] or [2-4]."""
        numbers: set[int] = set()
        for group in _CITATION.findall(self.text):
            for part in group.split(","):
                low, _, high = part.strip().replace("–", "-").partition("-")
                numbers.update(range(int(low), int(high or low) + 1))
        return [s for s in self.context.sources if s.index in numbers]


_CITATION = re.compile(r"\[(\d+(?:\s*[-–,]\s*\d+)*)\]")


def answer(question: str, builder: ContextBuilder, llm: LLM) -> Answer:
    """Retrieval-augmented generation in one call."""
    packet = builder.build(question)
    prompt = f"{packet.render()}\n\n<question>{question}</question>"
    return Answer(llm.text(system=ANSWER_SYSTEM, prompt=prompt), packet)
