"""Public documents hold findings and usage notes, not internal working notes."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_FILES = ["README.md", "CHANGELOG.md"]
INTERNAL_TERMS = [
    "advisor",
    "subagent",
    "after review",
    "reviewed by",
    "usage budget",
    "judgment call",
    "workplan",
    "TODO",
    "TBD",
    "Claude",
    "LLM",
    "/Users/",
]


@pytest.mark.parametrize("name", PUBLIC_FILES)
def test_public_documents_hold_no_internal_notes(name: str) -> None:
    text = (ROOT / name).read_text(encoding="utf-8")
    found = [t for t in INTERNAL_TERMS if re.search(re.escape(t), text, re.IGNORECASE)]
    assert found == []


def test_the_license_and_the_package_metadata_agree() -> None:
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "MIT"' in pyproject


def test_every_local_link_in_the_readme_points_at_a_file_that_exists() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\((?!https?://|#)([^)#]+)", text)
    assert links
    assert [link for link in links if not (ROOT / link).exists()] == []


# ---- ASD-STE100 style checks for the README (rules from docs/asd-ste.yaml) ----

MAX_WORDS_IN_SENTENCE = 25
MAX_SENTENCES_IN_PARAGRAPH = 6
LATIN_ABBREVIATIONS = ["e.g.", "i.e.", "etc.", "vs.", "cf.", "viz."]
REPLACE_PHRASES = [
    "in order to",
    "prior to",
    "due to the fact that",
    "in the event that",
    "at this point in time",
    "is able to",
    "has the ability to",
    "a large number of",
    "utilize",
    "carry out",
    "make an adjustment to",
    "perform a review of",
]
GENDERED_PRONOUNS = r"\b(he|she|his|hers|him|himself|herself)\b"
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def prose_units(markdown: str) -> list[str]:
    """Return the prose of a Markdown file: a unit for each paragraph, list item, or cell."""
    units: list[str] = []
    in_code = False
    current: list[str] = []

    def flush() -> None:
        if current:
            units.append(" ".join(current))
            current.clear()

    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            flush()
            in_code = not in_code
            continue
        if in_code or line.startswith("#") or set(line) <= {"|", "-", " "}:
            flush()
            continue
        if line.startswith("|"):
            flush()
            units.extend(cell.strip() for cell in line.strip("|").split("|") if cell.strip())
            continue
        if not line or re.match(r"^(\d+\.|[-*])\s", line):
            flush()
            if not line:
                continue
        current.append(re.sub(r"^(\d+\.|[-*])\s+", "", line))
    flush()
    return [clean_inline(u) for u in units]


def clean_inline(text: str) -> str:
    text = re.sub(r"`[^`]*`", "X", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<https?://[^>]*>", "X", text)
    return text.replace("**", "")


def sentences(unit: str) -> list[str]:
    return [s.strip() for s in SENTENCE_END.split(unit) if s.strip()]


def test_the_readme_follows_the_simplified_english_rules() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    units = prose_units(text)
    assert len(units) > 50
    problems: list[str] = []
    for unit in units:
        if ";" in unit:
            problems.append(f"semicolon: {unit[:70]}")
        for phrase in LATIN_ABBREVIATIONS + REPLACE_PHRASES:
            if phrase in unit.lower():
                problems.append(f"phrase '{phrase}': {unit[:70]}")
        if re.search(GENDERED_PRONOUNS, unit, re.IGNORECASE):
            problems.append(f"gendered pronoun: {unit[:70]}")
        parts = sentences(unit)
        if len(parts) > MAX_SENTENCES_IN_PARAGRAPH:
            problems.append(f"{len(parts)} sentences: {unit[:70]}")
        problems.extend(
            f"{len(s.split())} words: {s[:90]}"
            for s in parts
            if len(s.split()) > MAX_WORDS_IN_SENTENCE
        )
    assert problems == []
