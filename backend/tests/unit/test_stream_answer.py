"""qa.prepare_stream / stream_answer without a cache: pure streaming semantics."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.services.llm import Completion, LLMConfigError, LLMUnavailableError
from app.services.qa import (
    NO_CONTEXT_ANSWER,
    REPLAY_CHARS,
    Answer,
    prepare_stream,
    stream_answer,
)
from app.services.retriever import ChunkResult


def _chunk() -> ChunkResult:
    return ChunkResult(
        text="Life is 42.", document_id=uuid.uuid4(), chunk_index=0, filename="f.pdf", score=0.1
    )


class _LLM:
    model = "m"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def complete(self, system: str, user: str) -> Completion:  # pragma: no cover
        raise AssertionError

    def stream(self, system: str, user: str) -> Iterator[str | Completion]:
        yield "a "
        if self.fail:
            raise LLMUnavailableError("down")
        yield "b "
        yield Completion("a b ", 7, 2, None)


async def _run(prep: object) -> tuple[list[str], Answer]:
    deltas: list[str] = []
    final: Answer | None = None
    async for item in stream_answer(prep):  # type: ignore[arg-type]
        if isinstance(item, str):
            deltas.append(item)
        else:
            final = item
    assert final is not None
    return deltas, final


async def test_live_stream_yields_deltas_then_stripped_final_with_usage() -> None:
    prep = await prepare_stream("q", [_chunk()], _LLM())  # type: ignore[arg-type]
    deltas, final = await _run(prep)
    assert deltas == ["a ", "b "]
    assert (final.answer, final.prompt_tokens, final.completion_tokens, final.cost_usd) == (
        "a b",
        7,
        2,
        None,
    )


async def test_failure_raises_after_the_delivered_deltas() -> None:
    prep = await prepare_stream("q", [_chunk()], _LLM(fail=True))  # type: ignore[arg-type]
    got: list[str] = []
    with pytest.raises(LLMUnavailableError):
        async for item in stream_answer(prep):
            assert isinstance(item, str)
            got.append(item)
    assert got == ["a "]


async def test_no_context_is_replayed_in_small_events_without_an_llm() -> None:
    prep = await prepare_stream("q", [], None)
    deltas, final = await _run(prep)
    assert "".join(deltas) == NO_CONTEXT_ANSWER == final.answer
    assert all(len(d) <= REPLAY_CHARS for d in deltas) and len(deltas) > 1
    assert final.prompt_tokens is None  # nothing was called: nothing is known


async def test_context_without_an_llm_fails_before_streaming() -> None:
    with pytest.raises(LLMConfigError):
        await prepare_stream("q", [_chunk()], None)
