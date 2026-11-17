"""Exercise offline content validation and version-preserving SQLite import."""

import json
import shutil
import sqlite3
from contextlib import closing
from importlib.resources import files

import pytest

from daily_ml import bank
from daily_ml.database import Database


@pytest.fixture
def candidate(tmp_path):
    """Give each test an independently editable copy of the packaged bank."""
    return shutil.copytree(files("daily_ml").joinpath("resources"), tmp_path / "bank")


def _change(path, edit):
    """Apply a mutation to a JSON source record."""
    document = json.loads(path.read_text(encoding="utf-8"))
    edit(document)
    path.write_text(json.dumps(document), encoding="utf-8")


def _record(candidate, question_id="generalisation"):
    """Locate the individual JSON file by its stable ID."""
    return candidate / "questions" / f"{question_id}.json"


def test_release_bank_has_the_approved_allocation():
    """Check exact topic IDs and introductory difficulty from the bundle."""
    questions = bank.validate_bank().questions
    assert {question.id for question in questions} == {
        "generalisation", "data-leakage", "evaluation-metrics", "forest-mechanics",
        "tree-diversity", "bagging-vs-boosting", "boosting-tradeoffs", "backpropagation",
        "deep-learning-overfitting", "batch-vs-online", "reproducible-preprocessing",
        "production-monitoring",
    }
    assert {question.version for question in questions} == {1}
    assert {question.difficulty for question in questions} == {"introductory"}
    assert all(not question.retired for question in questions)


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        ("id", "invalid ID", "id"), ("id", "renamed", "filename"),
        ("version", 0, "version"), ("version", "1", "version"), ("version", True, "version"),
        ("section_id", "forests", "section_id"), ("domain_id", "mlops", "domain_id"),
        ("concept_id", "missing", "concept_id"), ("difficulty", "random", "difficulty"),
        ("question_type", "", "question_type"), ("tags", [], "tags"),
        ("title", " ", "title"), ("prompt", "", "prompt"),
        ("objective", "  ", "objective"), ("expected_scope", "", "expected_scope"),
        ("expected_answer_words", [100, 50], "expected_answer_words"),
        ("hint", "", "hint"), ("reference_explanation", "", "reference_explanation"),
        ("critical_misconceptions", [], "critical_misconceptions"), ("sources", [], "sources"),
        ("criteria", [], "criteria"), ("retired", 0, "retired"),
    ],
)
def test_bad_question_field_identifies_the_record(candidate, field, value, diagnostic):
    """Reject missing text, invalid identity, metadata and broken curriculum links."""
    _change(_record(candidate), lambda doc: doc.update({field: value}))
    with pytest.raises(bank.BankError, match=f"generalisation.*{diagnostic}"):
        bank.validate_bank(candidate)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "http://example.org/page"), ("url", "javascript:alert(1)"),
        ("url", "https://example.org@127.0.0.1/private"),
        ("url", "https://localhost/private"), ("url", "https://127.0.0.1/private"),
        ("url", "https://[::1]/private"), ("url", "https://example.org\\@other.org"),
        ("url", "https://example.org/a b"), ("url", "https://example.org:99999/page"),
        ("url", "https://0x7f000001/"), ("url", "https://example.internal/page"),
        ("title", ""), ("id", ""), ("reviewed_on", "2026-02-30"),
        ("reviewed_on", "20261117"), ("supports", []), ("supports", ["unknown-criterion"]),
    ],
)
def test_source_metadata_is_validated_offline(candidate, field, value):
    """Require dated objective/criterion evidence and public HTTPS source links."""
    _change(_record(candidate), lambda doc: doc["sources"][0].update({field: value}))
    with pytest.raises(bank.BankError, match=f"generalisation.*sources.*{field}"):
        bank.validate_bank(candidate)


@pytest.mark.parametrize("weight", ["0", "-0.1", "NaN", "Infinity", True])
def test_criteria_weights_must_be_positive_finite_decimals(candidate, weight):
    """Reject non-positive, non-finite and boolean scoring weights."""
    _change(_record(candidate), lambda doc: doc["criteria"][0].update(weight=weight))
    with pytest.raises(bank.BankError, match="generalisation.*weight"):
        bank.validate_bank(candidate)


@pytest.mark.parametrize(("offset", "accepted"), [("0.000001", True), ("0.0000010001", False)])
def test_decimal_weight_tolerance_includes_the_boundary(candidate, offset, accepted):
    """Check an exact Decimal tolerance boundary without float rounding."""
    from decimal import Decimal

    def edit(doc):
        criterion = doc["criteria"][0]
        criterion["weight"] = str(Decimal(str(criterion["weight"])) + Decimal(offset))

    _change(_record(candidate), edit)
    if accepted:
        assert bank.validate_bank(candidate)
    else:
        with pytest.raises(bank.BankError, match="generalisation.*criteria.*sum"):
            bank.validate_bank(candidate)


@pytest.mark.parametrize("problem", ["too_many", "duplicate", "unreviewed"])
def test_criteria_have_distinct_ids_and_evidence(candidate, problem):
    """Reject oversized rubrics, duplicates and criteria without review evidence."""
    def edit(doc):
        if problem == "too_many":
            doc["criteria"] *= 9
        elif problem == "duplicate":
            doc["criteria"][1]["id"] = doc["criteria"][0]["id"]
        else:
            doc["sources"][0]["supports"].remove(doc["criteria"][0]["id"])

    _change(_record(candidate), edit)
    with pytest.raises(bank.BankError, match="generalisation.*criteria"):
        bank.validate_bank(candidate)


def test_unique_ids_and_complete_release(candidate):
    """Detect duplicate identities and enforce all approved active starter topics."""
    original = _record(candidate)
    duplicate = original.with_name("copy.json")
    shutil.copyfile(original, duplicate)
    with pytest.raises(bank.BankError, match="generalisation.*duplicate"):
        bank.validate_bank(candidate)
    duplicate.unlink()
    original.unlink()
    with pytest.raises(bank.BankError, match="release.*generalisation"):
        bank.validate_bank(candidate)
    assert len(bank.validate_bank(candidate, release=False).questions) == 11


def test_invalid_curriculum_and_json_report_the_file(candidate):
    """Report the curriculum or question that cannot be read."""
    curriculum = candidate / "curriculum.json"
    original = curriculum.read_text(encoding="utf-8")
    _change(curriculum, lambda doc: doc["domains"].append(doc["domains"][0]))
    with pytest.raises(bank.BankError, match="curriculum.*duplicate"):
        bank.validate_bank(candidate)
    curriculum.write_text(original, encoding="utf-8")
    _record(candidate).write_text("{", encoding="utf-8")
    with pytest.raises(bank.BankError, match="generalisation.*JSON"):
        bank.validate_bank(candidate)
    with pytest.raises(bank.BankError, match="curriculum"):
        bank.validate_bank(candidate / "missing")


def test_unrepresentable_json_weight_reports_the_filename(candidate):
    """An exponent too large for Decimal must surface as a bank diagnostic."""
    path = _record(candidate)
    record = path.read_text(encoding="utf-8")
    number = '"weight": 1e9999999999999999999999999999'
    path.write_text(record.replace('"weight": "0.35"', number, 1), encoding="utf-8")

    with pytest.raises(bank.BankError, match=r"generalisation\.json.*JSON"):
        bank.validate_bank(candidate)


def _history(database):
    """Read immutable versions as a future history feature could."""
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT question_id, version, record_json FROM bank_question_versions ORDER BY question_id, version"
        ).fetchall()
    return {(key, version): json.loads(record) for key, version, record in rows}


def test_import_validates_and_rolls_back_immutable_version_conflicts(candidate, tmp_path):
    """Keep SQLite unchanged when a later record fails, even after earlier writes."""
    database = Database(tmp_path / "data")
    original = bank.validate_bank(candidate)
    assert bank.import_bank(database, original).inserted == 12
    assert bank.import_bank(database, original).inserted == 0
    before = _history(database)

    _change(_record(candidate, "backpropagation"), lambda doc: doc.update(version=2, prompt="An update."))
    last = _record(candidate, "tree-diversity")
    _change(last, lambda doc: doc.update(prompt="Changed without a version bump."))
    with pytest.raises(bank.BankError, match="tree-diversity.*version 1.*immutable"):
        bank.import_bank(database, bank.validate_bank(candidate))
    assert _history(database) == before

    _change(last, lambda doc: doc.update(version=2, prompt=" "))
    with pytest.raises(bank.BankError, match="tree-diversity.*prompt"):
        bank.import_bank(database, bank.validate_bank(candidate))
    assert _history(database) == before

    unsafe = original.questions[0].model_copy(update={"prompt": ""})
    with pytest.raises(bank.BankError, match=r"bank: questions\.0\.prompt"):
        bank.import_bank(database, original.model_copy(update={"questions": (unsafe, *original.questions[1:])}))
    assert _history(database) == before


def test_release_replacement_requires_retirement_and_preserves_history(candidate, tmp_path):
    """A replacement gives existing and fresh installations the same active prompts."""
    upgraded = Database(tmp_path / "upgraded")
    bank.import_bank(upgraded, bank.validate_bank(candidate))
    before = _history(upgraded)

    original_path = _record(candidate)
    replacement_path = original_path.with_name("new-generalisation.json")
    original_path.rename(replacement_path)
    _change(replacement_path, lambda doc: doc.update(id="new-generalisation", prompt="What does generalisation mean?"))
    _change(_record(candidate, "backpropagation"), lambda doc: doc.update(version=2))
    incomplete_upgrade = bank.validate_bank(candidate)

    with pytest.raises(bank.BankError, match="generalisation.*retirement"):
        bank.import_bank(upgraded, incomplete_upgrade)
    assert _history(upgraded) == before

    original_path.write_text(json.dumps(before["generalisation", 1]), encoding="utf-8")
    _change(original_path, lambda doc: doc.update(version=2, retired=True))
    complete_release = bank.validate_bank(candidate)
    fresh = Database(tmp_path / "fresh")
    assert bank.import_bank(upgraded, complete_release).inserted == 3
    assert bank.import_bank(fresh, complete_release).inserted == 13

    sections = bank.list_sections(fresh)
    assert sections == bank.list_sections(upgraded)
    assert [section.question_count for section in sections] == [3, 2, 2, 2, 1, 2]
    for section in sections:
        assert bank.list_questions(fresh, section.id) == bank.list_questions(upgraded, section.id)
    assert {question.id for question in bank.list_questions(upgraded, "fundamentals")} == {
        "data-leakage", "evaluation-metrics", "new-generalisation",
    }
    history = _history(upgraded)
    assert history["generalisation", 1] == before["generalisation", 1]
    assert history["generalisation", 2]["retired"] is True
    assert bank.import_bank(upgraded, complete_release).inserted == 0


def test_update_and_retirement_keep_original_versions_through_backup(candidate, tmp_path):
    """Check latest active prompts and equality after SQLite backup and restore."""
    database = Database(tmp_path / "data")
    original = bank.validate_bank(candidate)
    assert bank.import_bank(database, original).inserted == 12

    path = _record(candidate)
    _change(path, lambda doc: doc.update(version=2, prompt="How would you test generalisation?"))
    assert bank.import_bank(database, bank.validate_bank(candidate)).inserted == 1
    questions = bank.list_questions(database, "fundamentals")
    generalisation = next(question for question in questions if question.id == "generalisation")
    assert generalisation.version == 2
    assert generalisation.prompt == "How would you test generalisation?"
    with pytest.raises(bank.BankError, match="generalisation.*active version 1"):
        bank.import_bank(database, original)

    _change(path, lambda doc: doc.update(version=3, retired=True))
    with pytest.raises(bank.BankError, match="release.*generalisation"):
        bank.validate_bank(candidate)
    retired = bank.validate_bank(candidate, release=False)
    assert bank.import_bank(database, retired).inserted == 1
    history = _history(database)
    with pytest.raises(bank.BankError, match="generalisation.*active version 1"):
        bank.import_bank(database, original)
    assert _history(database) == history
    assert "generalisation" not in {question.id for question in bank.list_questions(database, "fundamentals")}
    assert bank.list_sections(database)[0].question_count == 2
    assert len(history) == 14
    assert history["generalisation", 1]["prompt"] != generalisation.prompt
    assert history["generalisation", 2]["prompt"] == generalisation.prompt
    assert history["generalisation", 3]["retired"] is True

    database.save_preferences({"timezone": "UTC"}, expected_version=0)
    restored = Database(tmp_path / "restored")
    with closing(sqlite3.connect(tmp_path / "backup.sqlite3")) as backup:
        with database.connect() as source:
            source.backup(backup)
        with restored.connect() as destination:
            backup.backup(destination)
    assert _history(restored) == history
    assert bank.list_questions(restored, "fundamentals") == bank.list_questions(database, "fundamentals")
    assert restored.get_preferences() == database.get_preferences()


def test_empty_and_unknown_sections(tmp_path, candidate):
    """Distinguish a known empty section from an unknown section."""
    database = Database(tmp_path / "data")
    _record(candidate, "batch-vs-online").unlink()
    bank.import_bank(database, bank.validate_bank(candidate, release=False))
    assert bank.list_questions(database, "deployment") == []
    with pytest.raises(bank.SectionNotFound):
        bank.list_questions(database, "unknown")
