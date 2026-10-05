import json

from validator import (
    FALLBACK_ANSWER,
    SourceDoc,
    extract_sources,
    refusal,
    render,
    strip_trailer,
    validate_answer,
)

KB_OUTPUT = json.dumps(
    [
        {
            "ref_id": 0,
            "content": json.dumps(
                {
                    "source_id": "mq-mplus-0000001-1",
                    "title": "A1C: Do you have information about A1C",
                    "url": "https://www.nlm.nih.gov/medlineplus/a1c.html",
                    "content": "A1C is a blood test...",
                }
            ),
        },
        {"ref_id": 1, "source_id": "mq-niddk-0000123-2", "title": "Diabetes: What causes it?", "url": ""},
    ]
)


def test_extract_sources_from_nested_json():
    docs = extract_sources([KB_OUTPUT])
    assert set(docs) == {"mq-mplus-0000001-1", "mq-niddk-0000123-2"}
    assert docs["mq-mplus-0000001-1"].url.startswith("https://www.nlm.nih.gov")
    assert docs["mq-niddk-0000123-2"].title == "Diabetes: What causes it?"


def test_extract_sources_from_plain_text():
    docs = extract_sources(["[mq-cancer-0000043v1-4] some text and mq-cdc-0000010-1"])
    assert set(docs) == {"mq-cancer-0000043v1-4", "mq-cdc-0000010-1"}


def test_grounded_answer_passes():
    docs = extract_sources([KB_OUTPUT])
    result = validate_answer("A1C measures average blood glucose [mq-mplus-0000001-1].", docs)
    assert result.evidence_status == "grounded"
    assert result.validation == "ok"
    assert result.cited == ["mq-mplus-0000001-1"]


def test_fabricated_citation_is_removed():
    docs = extract_sources([KB_OUTPUT])
    answer = "A1C measures glucose [mq-mplus-0000001-1]. It is perfect [mq-cdc-9999999-1]."
    result = validate_answer(answer, docs)
    assert result.validation == "repaired_citations"
    assert result.removed_citations == ["mq-cdc-9999999-1"]
    assert "mq-cdc-9999999-1" not in result.answer


def test_uncited_answer_is_blocked():
    docs = extract_sources([KB_OUTPUT])
    result = validate_answer("A1C is a blood test.", docs)
    assert result.validation == "blocked_ungrounded"
    assert result.answer == FALLBACK_ANSWER


def test_answer_citing_only_unretrieved_docs_is_blocked():
    result = validate_answer("Something [mq-cdc-0000001-1].", {})
    assert result.validation == "blocked_ungrounded"
    assert result.removed_citations == ["mq-cdc-0000001-1"]


def test_abstention_is_preserved():
    result = validate_answer("I don\u2019t have enough information in the knowledge base to answer that reliably.", {})
    assert result.evidence_status == "insufficient"
    assert result.validation == "ok"


def test_model_sources_section_is_replaced():
    docs = extract_sources([KB_OUTPUT])
    answer = "A1C is a test [mq-mplus-0000001-1].\n\n**Sources:**\n- made up"
    result = validate_answer(answer, docs)
    assert "made up" not in result.answer


def test_render_roundtrip_trailer():
    docs = {"mq-mplus-0000001-1": SourceDoc("mq-mplus-0000001-1", "A1C", "https://example.org")}
    result = validate_answer("A1C [mq-mplus-0000001-1].", docs)
    text = render(result, docs, {"prompt_version": "1.0.0"})
    body, meta = strip_trailer(text)
    assert "**Sources**" in body
    assert "[A1C](https://example.org)" in body
    assert meta["evidence_status"] == "grounded"
    assert meta["cited"] == ["mq-mplus-0000001-1"]
    assert meta["prompt_version"] == "1.0.0"


def test_refusal_trailer():
    body, meta = strip_trailer(refusal("No PHI please.", "phi_ssn", {"prompt_version": "1"}))
    assert body.startswith("No PHI please.")
    assert meta["evidence_status"] == "refused"
    assert meta["refusal_reason"] == "phi_ssn"


def test_strip_trailer_without_trailer():
    assert strip_trailer("hello") == ("hello", None)
