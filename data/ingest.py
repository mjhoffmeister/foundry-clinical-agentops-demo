"""Build the clinical knowledge corpus from MedQuAD.

MedQuAD (Ben Abacha & Demner-Fushman, 2019) is licensed CC BY 4.0 and contains
question/answer pairs curated from NIH websites. This script:

  * clones https://github.com/abachaa/MedQuAD (shallow) into data/.cache,
  * keeps a focused slice of common-condition collections,
  * EXCLUDES subsets 10 (A.D.A.M.), 11 (MedlinePlus Drugs) and 12 (MedlinePlus
    Herbs & Supplements) whose answers were removed for copyright reasons,
  * writes one search document per QA pair to data/corpus.jsonl with a stable,
    regex-distinctive ``source_id`` (``mq-<collection>-<docid>-<pid>``).

The content is historical NIH reference material, not current clinical
guidance. Run:  python data/ingest.py
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache" / "MedQuAD"
OUT = ROOT / "corpus.jsonl"
REPO = "https://github.com/abachaa/MedQuAD.git"

# collection folder -> short code used in source ids
COLLECTIONS: dict[str, str] = {
    "4_MPlus_Health_Topics_QA": "mplus",
    "5_NIDDK_QA": "niddk",
    "6_NINDS_QA": "ninds",
    "7_SeniorHealth_QA": "senior",
    "8_NHLBI_QA_XML": "nhlbi",
    "9_CDC_QA": "cdc",
    "1_CancerGov_QA": "cancer",
}
EXCLUDED = {"10_MPlus_ADAM_QA", "11_MPlusDrugs_QA", "12_MPlusHerbsSupplements_QA"}
MAX_CONTENT_CHARS = 6000

SOURCE_ID_RE = re.compile(r"mq-[a-z]+-[0-9a-z]+-\d+")


def ensure_dataset() -> None:
    if CACHE.exists():
        return
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--depth", "1", REPO, str(CACHE)], check=True)


def clean(text: str | None) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def parse_file(path: Path, code: str) -> list[dict]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []
    doc_id = re.sub(r"[^0-9a-z]", "v", root.get("id", "").strip().lower())
    url = (root.get("url") or "").strip()
    source = (root.get("source") or code).strip()
    focus = clean(root.findtext("Focus"))
    rows: list[dict] = []
    for pair in root.iter("QAPair"):
        pid = (pair.get("pid") or "").strip()
        question_el = pair.find("Question")
        question = clean(question_el.text if question_el is not None else "")
        qtype = question_el.get("qtype", "") if question_el is not None else ""
        answer = clean(pair.findtext("Answer"))
        if not (doc_id and pid and question and answer) or len(answer) < 40:
            continue
        source_id = f"mq-{code}-{doc_id}-{pid}"
        assert SOURCE_ID_RE.fullmatch(source_id), source_id
        rows.append(
            {
                "id": source_id,
                "source_id": source_id,
                "title": f"{focus}: {question}" if focus else question,
                "focus": focus,
                "question": question,
                "qtype": qtype,
                "content": answer[:MAX_CONTENT_CHARS],
                "url": url,
                "collection": source,
            }
        )
    return rows


def main() -> int:
    ensure_dataset()
    seen_answers: set[str] = set()
    corpus: list[dict] = []
    for folder, code in COLLECTIONS.items():
        assert folder not in EXCLUDED
        files = sorted((CACHE / folder).glob("*.xml"))
        kept = 0
        for path in files:
            for row in parse_file(path, code):
                key = row["content"][:500]
                if key in seen_answers:
                    continue
                seen_answers.add(key)
                corpus.append(row)
                kept += 1
        print(f"{folder:<28} files={len(files):>5} qa_kept={kept:>5}")
    OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in corpus) + "\n", encoding="utf-8")
    print(f"wrote {len(corpus)} documents -> {OUT.relative_to(ROOT.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
