from pathlib import Path
from types import SimpleNamespace

import pytest

from contextlayer.agent import ContextAgent
from contextlayer.context import ContextBuilder
from contextlayer.evaluation import GoldenItem, evaluate, precision_at_k, recall_at_k, reciprocal_rank
from contextlayer.memory import MemoryStore
from contextlayer.retrieval import Retriever
from contextlayer.text2surql import GuardrailError, TextToSurql, check_query, schema_prompt

EXAMPLES = Path(__file__).parent.parent / "data" / "surql_examples.json"


@pytest.mark.parametrize("query", [
    "SELECT * FROM document; DELETE document",
    "DELETE document",
    "SELECT * FROM (DELETE document)",
    "SELECT http::get('https://example.com') FROM document",
    "LET $x = 1",
    "INFO FOR DB",
    "SELECT email, password FROM user",
    "SELECT content FROM turn",
    "SELECT * FROM memory WHERE owner != NONE",
    "SELECT ->supersedes->memory FROM memory",
])
def test_guardrails_reject(query):
    with pytest.raises(GuardrailError):
        check_query(query)


@pytest.mark.parametrize("query", [
    "SELECT * FROM document",
    "SELECT * FROM document WHERE title = 'DELETE; DROP'",
    "select title from document;",
])
def test_guardrails_allow(query):
    check_query(query)


def test_schema_prompt_hides_internals(corpus_db):
    prompt = schema_prompt(corpus_db)
    assert "DEFINE TABLE document" in prompt and "DEFINE FIELD title ON document" in prompt
    assert "embedding" not in prompt and "password" not in prompt
    assert "PERMISSIONS" not in prompt and "surql_example" not in prompt


class ScriptedLLM:
    """Returns canned responses in order and records the prompts it was given."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def json(self, *, system, prompt, schema):
        self.prompts.append(prompt)
        return self.responses.pop(0)

    def create(self, *, system, messages, tools=None, max_tokens=16000):
        self.prompts.append(messages[-1])
        return self.responses.pop(0)


def test_text2surql_retries_with_the_error(corpus_db, embedder):
    llm = ScriptedLLM([
        {"query": "SELECT count() FROM documents GROUP ALL", "explanation": "typo"},
        {"query": "DELETE document", "explanation": "bad"},
        {"query": "SELECT count() FROM document WHERE kind = 'runbook' GROUP ALL", "explanation": "ok"},
    ])
    t = TextToSurql(corpus_db, llm, embedder)
    t.load_examples(EXAMPLES)
    result = t.ask("how many runbooks?")
    assert result.ok and result.rows == [{"count": 3}]
    assert len(result.attempts) == 2
    assert "documents" in llm.prompts[1] and "does not exist" in llm.prompts[1]
    assert "<examples>" in llm.prompts[0]


def _block(type, **kw):
    return SimpleNamespace(type=type, **kw)


def test_agent_runs_tools_and_returns_final_text(corpus_db, embedder):
    llm = ScriptedLLM([
        SimpleNamespace(stop_reason="tool_use", content=[
            _block("text", text="Let me look."),
            _block("tool_use", id="t1", name="search_knowledge", input={"query": "ERR_CHECKOUT_429"}),
            _block("tool_use", id="t2", name="remember",
                   input={"content": "The user works on checkout.", "kind": "fact", "importance": 0.6}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[_block("text", text="It means rate limited [Rate limiting].")]),
    ])
    memory = MemoryStore(corpus_db, embedder, agent="assistant")
    agent = ContextAgent(llm, ContextBuilder(Retriever(corpus_db, embedder), memory), memory)
    turn = agent.send("I work on checkout. What is ERR_CHECKOUT_429?")

    assert turn.text.startswith("It means rate limited")
    assert [c.name for c in turn.tool_calls] == ["search_knowledge", "remember"]
    assert "Rate limiting" in turn.tool_calls[0].output
    assert [m.content for m in memory.all()] == ["The user works on checkout."]
    results = agent.messages[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"], "all results go back in one message"


def test_metrics():
    assert recall_at_k(["a", "b", "c"], ["b", "z"], 2) == 0.5
    assert precision_at_k(["a", "b", "c"], ["b", "z"], 2) == 0.5
    assert reciprocal_rank(["a", "b", "c"], ["c"]) == pytest.approx(1 / 3)
    assert reciprocal_rank(["a"], ["c"]) == 0.0


def test_python_and_database_metrics_agree(db):
    golden = [GoldenItem("q1", ["document:a", "document:b"], ["x"]), GoldenItem("q2", ["document:c"], ["y"])]
    ranked = {"q1": ["document:b", "document:z", "document:a"], "q2": ["document:z", "document:c"]}
    py = evaluate("py", ranked.get, golden, k=2)
    in_db = evaluate("db", ranked.get, golden, k=2, db=db)
    assert py.overall == in_db.overall


def test_agent_correction_supersedes_and_cites_the_turn(corpus_db, embedder):
    def remember_call(id, content):
        return SimpleNamespace(stop_reason="tool_use", content=[_block(
            "tool_use", id=id, name="remember",
            input={"content": content, "kind": "fact", "importance": 0.7, "subject": "user", "attribute": "team"},
        )])

    done = SimpleNamespace(stop_reason="end_turn", content=[_block("text", text="Noted.")])
    llm = ScriptedLLM([remember_call("t1", "The user is on the payments team."), done,
                       remember_call("t2", "The user is on the platform team."), done])
    memory = MemoryStore(corpus_db, embedder, agent="assistant")
    agent = ContextAgent(llm, ContextBuilder(Retriever(corpus_db, embedder), memory), memory)

    agent.send("I'm on the payments team.")
    turn = agent.send("Correction: I moved to the platform team.")

    assert "supersedes" in turn.tool_calls[0].output
    current = memory.lookup("user", "team")
    assert current.content == "The user is on the platform team."
    assert corpus_db.one(f"SELECT VALUE content FROM ONLY {current.source_turn}") == "Correction: I moved to the platform team."
    assert [m.content for m in memory.history("user", "team")] == [
        "The user is on the payments team.", "The user is on the platform team.",
    ]
