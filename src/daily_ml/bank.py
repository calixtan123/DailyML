"""Validate the reviewed curriculum and persist immutable question versions."""

import json
import re
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from sqlite3 import Connection
from typing import Annotated, Literal, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator

from .database import Database

ID_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
_MAX_ID_LENGTH = 64
_WEIGHT_TOLERANCE = Decimal("0.000001")
_WEIGHT_DIGITS = 28
_STARTER_CONCEPTS = {
    "fundamentals": {"generalisation", "data-leakage", "evaluation-metrics"},
    "forests": {"forest-mechanics", "tree-diversity"},
    "other-ensembles": {"bagging-vs-boosting", "boosting-tradeoffs"},
    "deep-learning": {"backpropagation", "deep-learning-overfitting"},
    "deployment": {"batch-vs-online"},
    "mlops": {"reproducible-preprocessing", "production-monitoring"},
}

EditorialText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_ID = Annotated[str, StringConstraints(pattern=ID_PATTERN, max_length=_MAX_ID_LENGTH)]
_POSITIVE_INT = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
_DIFFICULTY = Literal["introductory", "intermediate", "advanced"]
_WEIGHT = Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=_WEIGHT_DIGITS)]

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS bank_sections (
        id TEXT PRIMARY KEY,
        domain_id TEXT NOT NULL,
        title TEXT NOT NULL,
        position INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bank_question_versions (
        question_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        section_id TEXT NOT NULL REFERENCES bank_sections (id),
        concept_id TEXT NOT NULL,
        retired INTEGER NOT NULL CHECK (retired IN (0, 1)),
        record_json TEXT NOT NULL,
        PRIMARY KEY (question_id, version)
    )
    """,
    """
    CREATE VIEW IF NOT EXISTS bank_active_questions AS
    SELECT current.*
    FROM bank_question_versions AS current
    WHERE current.retired = 0 AND NOT EXISTS (
        SELECT 1 FROM bank_question_versions AS newer
        WHERE newer.question_id = current.question_id AND newer.version > current.version
    )
    """,
)


class BankError(ValueError):
    """A curriculum or question record is invalid or conflicts with saved content."""


class SectionNotFound(LookupError):
    """A requested section is absent from the imported curriculum."""


class _ValidatedRecord(BaseModel):
    """Require declared fields and enforce validation even on copied models."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class _Section(_ValidatedRecord):
    """An ordered topic section with the concepts it owns."""

    id: _ID
    title: EditorialText
    concepts: Annotated[tuple[_ID, ...], Field(min_length=1)]


class _Domain(_ValidatedRecord):
    """A learning domain and its ordered topic sections."""

    id: _ID
    sections: Annotated[tuple[_Section, ...], Field(min_length=1)]


class _Curriculum(_ValidatedRecord):
    """The hierarchy used to validate question and concept membership."""

    domains: Annotated[tuple[_Domain, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique_ids(self) -> "_Curriculum":
        """Require unambiguous domain, section and concept IDs."""
        _require_unique((domain.id for domain in self.domains), "domain")
        sections = [section for domain in self.domains for section in domain.sections]
        _require_unique((section.id for section in sections), "section")
        _require_unique((concept for section in sections for concept in section.concepts), "concept")
        return self


class _Criterion(_ValidatedRecord):
    """A distinct, weighted rubric requirement."""

    id: _ID
    description: EditorialText
    weight: _WEIGHT


class _Source(_ValidatedRecord):
    """An editorial source and the objective or criteria it supports."""

    id: _ID
    title: EditorialText
    url: EditorialText
    reviewed_on: date
    supports: Annotated[tuple[_ID, ...], Field(min_length=1)]

    @field_validator("url", mode="before")
    @classmethod
    def _https_source(cls, value: object) -> object:
        """Accept public HTTPS links without user info or browser-ambiguous syntax."""
        if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError("must be an HTTPS URL without whitespace")
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError:
            raise ValueError("must be a valid HTTPS URL") from None
        host = parts.hostname or ""
        labels = host.lower().removesuffix(".").split(".")
        dns_label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        tld = labels[-1]
        if (
            parts.scheme != "https" or "\\" in value or "\x7f" in value
            or parts.username is not None or parts.password is not None
            or parts.netloc.endswith(":") or port == 0 or len(host) > 254 or len(labels) < 2
            or any(not re.fullmatch(dns_label, label) for label in labels)
            or not re.fullmatch(r"(?:[a-z]{2,63}|xn--[a-z0-9-]+)", tld)
            or tld in {"local", "localhost", "internal", "home", "lan", "test", "invalid", "onion"}
        ):
            raise ValueError("must be a public HTTPS URL without credentials")
        return value

    @field_validator("reviewed_on", mode="before")
    @classmethod
    def _iso_review_date(cls, value: object) -> object:
        """Require a calendar date written in ISO YYYY-MM-DD form."""
        if type(value) is date:
            return value
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("must be a YYYY-MM-DD date")
        return value


class _Question(_ValidatedRecord):
    """A complete, immutable question record including private assessment content."""

    id: _ID
    version: _POSITIVE_INT
    domain_id: _ID
    section_id: _ID
    concept_id: _ID
    tags: Annotated[tuple[_ID, ...], Field(min_length=1)]
    difficulty: _DIFFICULTY
    question_type: Literal["explanation", "comparison", "scenario"]
    title: EditorialText
    prompt: EditorialText
    objective: EditorialText
    expected_scope: EditorialText
    expected_answer_words: tuple[_POSITIVE_INT, _POSITIVE_INT]
    hint: EditorialText
    reference_explanation: EditorialText
    criteria: Annotated[tuple[_Criterion, ...], Field(min_length=1, max_length=8)]
    critical_misconceptions: Annotated[tuple[EditorialText, ...], Field(min_length=1)]
    sources: Annotated[tuple[_Source, ...], Field(min_length=1)]
    retired: Annotated[bool, Field(strict=True)]

    @model_validator(mode="after")
    def _consistent_content(self) -> "_Question":
        """Check response length, rubric total and complete source mapping."""
        if self.expected_answer_words[0] > self.expected_answer_words[1]:
            raise ValueError("expected_answer_words must run from minimum to maximum")
        _require_unique((criterion.id for criterion in self.criteria), "criteria")
        _require_unique((source.id for source in self.sources), "sources")
        required = {criterion.id for criterion in self.criteria}
        if "objective" in required:
            raise ValueError("criteria IDs must not use the reserved ID 'objective'")
        required.add("objective")
        reviewed = set()
        for source in self.sources:
            unknown = set(source.supports) - required
            if unknown:
                raise ValueError(f"sources.{source.id}.supports includes unknown IDs: {', '.join(sorted(unknown))}")
            reviewed.update(source.supports)
        if required - reviewed:
            raise ValueError(f"criteria or objective missing source review: {', '.join(sorted(required - reviewed))}")
        # Allow headroom for adding eight values and subtracting one exactly.
        with localcontext(prec=2 * _WEIGHT_DIGITS + 2):
            total = sum(criterion.weight for criterion in self.criteria)
            if abs(total - Decimal(1)) > _WEIGHT_TOLERANCE:
                raise ValueError(f"criteria weights must sum to 1 within {_WEIGHT_TOLERANCE}")
        return self


class QuestionBank(_ValidatedRecord):
    """A locally validated content set.

    Attributes
    ----------
    curriculum : _Curriculum
        Ordered domain, section and concept hierarchy.
    questions : tuple of _Question
        One checked version per stable question ID.
    release : bool
        Whether the approved starter coverage is required.
    """

    curriculum: _Curriculum
    questions: tuple[_Question, ...]
    release: bool


@dataclass(frozen=True)
class ImportSummary:
    """Count the content versions written by one successful import.

    Attributes
    ----------
    inserted : int
        Number of new version rows, including retirement versions.
    """

    inserted: int


@dataclass(frozen=True)
class SectionSummary:
    """Describe a section with its current number of available questions.

    Attributes
    ----------
    id : str
        Stable ID used to request the section's prompts.
    title : str
        Display title.
    question_count : int
        Number of latest, non-retired question versions.
    """

    id: str
    title: str
    question_count: int


@dataclass(frozen=True)
class QuestionPrompt:
    """Allowlist the fields visible before the learner requests help.

    Attributes
    ----------
    id : str
        Stable question ID.
    version : int
        Content version displayed.
    title : str
        Concise question name.
    prompt : str
        Public practice prompt.
    objective : str
        Public learning objective.
    difficulty : str
        Difficulty label.
    concept_id : str
        Stable primary concept for displaying its review schedule.
    """

    id: str
    version: int
    title: str
    prompt: str
    objective: str
    difficulty: _DIFFICULTY
    concept_id: str


def _require_unique(ids, label: str) -> None:
    """Reject the first repeated ID with a record-specific label."""
    seen = set()
    for identity in ids:
        if identity in seen:
            raise ValueError(f"duplicate {label} ID '{identity}'")
        seen.add(identity)


def _read_json(path: Traversable):
    """Read local JSON and retain the exact decimals written by the author."""
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except InvalidOperation:
        raise BankError(f"{path.name}: JSON number is outside the supported decimal range") from None
    except (OSError, UnicodeError, ValueError) as error:
        raise BankError(f"{path.name}: could not read valid UTF-8 JSON ({type(error).__name__})") from None


def _record_label(data: object, filename: str) -> str:
    """Use a safe record ID when available and always keep the filename."""
    identity = data.get("id") if isinstance(data, dict) else None
    if isinstance(identity, str) and len(identity) <= _MAX_ID_LENGTH and re.fullmatch(ID_PATTERN, identity):
        return f"{identity} ({filename})"
    return filename


_Record = TypeVar("_Record", bound=_ValidatedRecord)


def _validate_record(model: type[_Record], data: object, label: str) -> _Record:
    """Summarise schema failures without repeating invalid inputs."""
    try:
        return model.model_validate(data)
    except ValidationError as error:
        failures = [
            f"{'.'.join(str(part) for part in item['loc']) or 'record'}: {item['msg']}"
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        ]
        raise BankError(f"{label}: {' | '.join(failures)}") from None


def parse_question_snapshot(snapshot_json: str) -> _Question:
    """Parse one persisted immutable question snapshot using the bank contract.

    Parameters
    ----------
    snapshot_json : str
        JSON saved with a question version or practice item.

    Returns
    -------
    _Question
        The fully validated question record.

    Raises
    ------
    BankError
        If the snapshot is malformed or cannot form a supported question record.
    """
    try:
        snapshot = json.loads(snapshot_json)
    except (TypeError, json.JSONDecodeError):
        raise BankError("saved question snapshot is not valid JSON") from None
    return _validate_record(_Question, snapshot, "saved question snapshot")


def validate_saved_question_versions(connection: Connection, packaged_bank: QuestionBank | None = None) -> None:
    """Validate persisted immutable question versions against the bank contract.

    Parameters
    ----------
    connection : sqlite3.Connection
        Connection to a backup candidate containing ``bank_question_versions``.
    packaged_bank : QuestionBank or None
        Validated packaged bank used at application startup. When omitted, the
        package resources are loaded and validated.

    Raises
    ------
    BankError
        If a stored record is malformed, disagrees with its durable identity
        columns, or prevents the packaged bank from being imported at startup.
    """
    packaged = packaged_bank if packaged_bank is not None else validate_bank()
    # Run the same release import against an isolated bank. Only bank rows are
    # needed, and importing missing bundled versions must never edit a backup.
    with closing(Connection(":memory:")) as preview:
        for statement in _SCHEMA:
            preview.execute(statement)
        preview.executemany(
            "INSERT INTO bank_sections VALUES (?, ?, ?, ?)",
            connection.execute("SELECT id, domain_id, title, position FROM bank_sections"),
        )
        rows = connection.execute(
            """SELECT question_id, version, section_id, concept_id, retired, record_json
               FROM bank_question_versions"""
        )
        for row in rows:
            record = parse_question_snapshot(row[5])
            if (record.id, record.version, record.section_id, record.concept_id, int(record.retired)) != row[:5]:
                raise BankError("saved question version identity does not match its durable row")
            preview.execute("INSERT INTO bank_question_versions VALUES (?, ?, ?, ?, ?, ?)", row)
        _apply_bank(preview, packaged)


def _check_membership(bank: QuestionBank) -> None:
    """Check cross-file membership and the exact reviewed starter allocation."""
    locations = {
        concept: (domain.id, section.id)
        for domain in bank.curriculum.domains for section in domain.sections for concept in section.concepts
    }
    active: dict[str, list[str]] = {}
    for record in bank.questions:
        if record.concept_id not in locations:
            raise BankError(f"{record.id}: unknown concept_id '{record.concept_id}'")
        domain, section = locations[record.concept_id]
        if record.domain_id != domain:
            raise BankError(f"{record.id}: domain_id must be '{domain}' for concept '{record.concept_id}'")
        if record.section_id != section:
            raise BankError(f"{record.id}: section_id must be '{section}' for concept '{record.concept_id}'")
        if not record.retired:
            active.setdefault(record.section_id, []).append(record.concept_id)
    if bank.release:
        for section in sorted(_STARTER_CONCEPTS.keys() | active.keys()):
            expected = _STARTER_CONCEPTS.get(section, set())
            actual = active.get(section, [])
            if set(actual) != expected or len(actual) != len(expected):
                raise BankError(
                    f"release section '{section}': expected one active question per concept "
                    f"({', '.join(sorted(expected))}); found {', '.join(sorted(actual)) or 'none'}"
                )


def validate_bank(paths: Path | None = None, release: bool = True) -> QuestionBank:
    """Read and validate the reviewed question bank without network or SQLite.

    Parameters
    ----------
    paths : Path or None
        Candidate bank directory containing curriculum.json and questions/*.json.
        Defaults to the package resources, independent of the working directory.
    release : bool
        Require exactly the approved active starter concepts in their sections.

    Returns
    -------
    QuestionBank
        Validated curriculum and immutable question records in filename order.

    Raises
    ------
    BankError
        If JSON, content, curriculum membership or release coverage is invalid.
    """
    root = paths if paths is not None else files("daily_ml").joinpath("resources")
    curriculum = _validate_record(_Curriculum, _read_json(root.joinpath("curriculum.json")), "curriculum.json")
    try:
        paths_to_read = sorted(
            (entry for entry in root.joinpath("questions").iterdir() if entry.name.endswith(".json")),
            key=lambda entry: entry.name,
        )
    except OSError:
        raise BankError("questions/: could not list question records") from None
    questions = []
    paths_by_id = {}
    for path in paths_to_read:
        data = _read_json(path)
        record = _validate_record(_Question, data, _record_label(data, path.name))
        if record.id in paths_by_id:
            raise BankError(f"{record.id}: duplicate question ID in {paths_by_id[record.id]} and {path.name}")
        paths_by_id[record.id] = path.name
        questions.append(record)
    for identity, filename in paths_by_id.items():
        if filename != f"{identity}.json":
            raise BankError(f"{filename}: filename must be {identity}.json for question '{identity}'")
    bank = QuestionBank(curriculum=curriculum, questions=tuple(questions), release=release)
    _check_membership(bank)
    return bank


def _check_active_release(connection: Connection, bank: QuestionBank) -> None:
    """Require the uncommitted active versions to match the release bundle."""
    expected = {question.id: question.version for question in bank.questions if not question.retired}
    active = dict(connection.execute("SELECT question_id, version FROM bank_active_questions"))
    unretired = sorted(active.keys() - expected.keys())
    if unretired:
        raise BankError(f"{', '.join(unretired)}: saved questions are still active; include a higher retirement version")
    for identity, version in expected.items():
        if active.get(identity) != version:
            raise BankError(f"{identity}: release expects active version {version}, which would not be current")


def import_bank(database: Database, bank: QuestionBank) -> ImportSummary:
    """Import a completely validated bank in one SQLite transaction.

    Parameters
    ----------
    database : Database
        Local app database.
    bank : QuestionBank
        Candidate bank from validate_bank.

    Returns
    -------
    ImportSummary
        Number of newly stored versions. Reimporting unchanged versions inserts zero.

    Raises
    ------
    BankError
        If the bank is invalid, reuses a version with different content, changes
        the curriculum identity of a saved question, regresses its version or
        would leave active versions different from the strict release.
    sqlite3.Error
        If SQLite cannot commit the import. Prior rows remain unchanged.
    """
    # Pydantic's model_copy can bypass validation. Check before opening a transaction.
    checked = _validate_record(QuestionBank, bank, "bank")
    _check_membership(checked)
    try:
        _require_unique((question.id for question in checked.questions), "question")
    except ValueError as error:
        raise BankError(str(error)) from None

    with database.connect(immediate=True) as connection:
        for statement in _SCHEMA:
            connection.execute(statement)
        database.record_backup_tables(connection)
        return ImportSummary(_apply_bank(connection, checked))


def _apply_bank(connection: Connection, bank: QuestionBank) -> int:
    """Apply validated release content and check the resulting active versions."""
    inserted = 0
    sections = (
        (domain.id, section) for domain in bank.curriculum.domains for section in domain.sections
    )
    for position, (domain_id, section) in enumerate(sections):
        existing = connection.execute(
            "SELECT domain_id FROM bank_sections WHERE id = ?", (section.id,)
        ).fetchone()
        if existing and existing[0] != domain_id:
            raise BankError(f"{section.id}: cannot change the domain of a saved section")
        connection.execute(
            """INSERT INTO bank_sections (id, domain_id, title, position) VALUES (?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET title = excluded.title, position = excluded.position
               WHERE title != excluded.title OR position != excluded.position""",
            (section.id, domain_id, section.title, position),
        )
    for question in bank.questions:
        snapshot = question.model_dump_json()
        saved = connection.execute(
            """SELECT record_json FROM bank_question_versions
               WHERE question_id = ? AND version = ?""",
            (question.id, question.version),
        ).fetchone()
        if saved is not None:
            if saved[0] != snapshot:
                raise BankError(f"{question.id}: version {question.version} is immutable; increase version")
            continue
        prior = connection.execute(
            """SELECT version, section_id, concept_id FROM bank_question_versions
               WHERE question_id = ? ORDER BY version DESC LIMIT 1""",
            (question.id,),
        ).fetchone()
        if prior is not None:
            if question.version <= prior[0]:
                raise BankError(f"{question.id}: new version {question.version} must exceed version {prior[0]}")
            if (question.section_id, question.concept_id) != prior[1:]:
                raise BankError(f"{question.id}: cannot change the section or concept of a saved question")
        connection.execute(
            """INSERT INTO bank_question_versions
               (question_id, version, section_id, concept_id, retired, record_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (question.id, question.version, question.section_id, question.concept_id, question.retired, snapshot),
        )
        inserted += 1
    if bank.release:
        _check_active_release(connection, bank)
    return inserted


def list_sections(database: Database) -> list[SectionSummary]:
    """List imported sections in curriculum order with active question counts.

    Parameters
    ----------
    database : Database
        Local app database.

    Returns
    -------
    list of SectionSummary
        All known sections, including any with no currently available questions.

    Raises
    ------
    sqlite3.Error
        If SQLite cannot read the sections.
    """
    with database.connect() as connection:
        rows = connection.execute(
            """SELECT section.id, section.title, COUNT(question.question_id)
               FROM bank_sections AS section
               LEFT JOIN bank_active_questions AS question ON section.id = question.section_id
               GROUP BY section.id, section.title
               ORDER BY section.position, section.id"""
        ).fetchall()
    return [SectionSummary(*row) for row in rows]


def list_questions(database: Database, section_id: str) -> list[QuestionPrompt]:
    """List the current prompts for a known imported section.

    Parameters
    ----------
    database : Database
        Local app database.
    section_id : str
        Stable section ID.

    Returns
    -------
    list of QuestionPrompt
        Public question fields ordered by stable question ID.

    Raises
    ------
    SectionNotFound
        If the section is absent from the imported curriculum.
    sqlite3.Error
        If SQLite cannot read the prompts.
    """
    with database.connect() as connection:
        section = connection.execute("SELECT 1 FROM bank_sections WHERE id = ?", (section_id,)).fetchone()
        if section is None:
            raise SectionNotFound(section_id)
        rows = connection.execute(
            """SELECT question_id, version,
                      json_extract(record_json, '$.title'),
                      json_extract(record_json, '$.prompt'),
                      json_extract(record_json, '$.objective'),
                      json_extract(record_json, '$.difficulty'), concept_id
               FROM bank_active_questions
               WHERE section_id = ? ORDER BY question_id""",
            (section_id,),
        ).fetchall()
    return [QuestionPrompt(*row) for row in rows]
