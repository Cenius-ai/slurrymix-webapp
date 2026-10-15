#!/usr/bin/env python3
"""Slurrymix: manure slurry ratio calculator, spread rotation planner, storage tracker.

Everything is metric (m3, hectares, kg N), the tool is standard library only and
runs offline.  State lives in one human-readable JSON file (default
``./slurrymix.json``, override with ``--state PATH``) written atomically with a
temporary file plus :func:`os.replace`.

Failure contract:
    0   success
    1   domain error  (unknown record, duplicate name, over-capacity refusal)
    2   bad input     (unparseable or out-of-range flag value)
    130 interrupted   (Ctrl-C, nothing written)
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import functools
import json
import math
import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

PROG = "slurrymix"
VERSION = "1.0.0"
SCHEMA_VERSION = 1
STATE_FILENAME = "slurrymix.json"
SOURCES = ("cattle", "pig", "poultry", "other")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Design tokens for the committed direction: calm-precise CLI, terminal-dark
# surface, system monospace, ONE accent used for primary status only.
# accent oklch(0.58 0.12 209) == #008da0 (the hex is what non-CSS contexts use).
THEME = {
    "surface": "terminal-dark",
    "type": "system monospace",
    "accent_oklch": "oklch(0.58 0.12 209)",
    "accent_hex": "#008da0",
    "accent_ansi": "\x1b[38;2;0;141;160m",
    "bold_ansi": "\x1b[1m",
    "dim_ansi": "\x1b[2m",
    "reset_ansi": "\x1b[0m",
}

_COLOR = False


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class CliError(Exception):
    """Any failure the CLI reports to the operator instead of a traceback."""

    exit_code = 2

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class InputError(CliError):
    """Bad input: unparseable or out-of-range flag value (exit 2)."""

    exit_code = 2


class DomainError(CliError):
    """A valid request the tool refuses: unknown record, duplicate, over budget (exit 1)."""

    exit_code = 1


class StateError(CliError):
    """The state file cannot be used: corrupt, unreadable or too new (exit 1)."""

    exit_code = 1


class WizardAbort(CliError):
    """The guided wizard lost its input; nothing has been written (exit 130)."""

    exit_code = 130


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #
def style(text: str, *codes: str) -> str:
    if not _COLOR or not codes:
        return text
    return "".join(codes) + text + THEME["reset_ansi"]


def accent(text: str) -> str:
    return style(text, THEME["accent_ansi"])


def bold(text: str) -> str:
    return style(text, THEME["bold_ansi"])


def dim(text: str) -> str:
    return style(text, THEME["dim_ansi"])


def out(text: str = "") -> None:
    print(text)


def err(text: str = "") -> None:
    print(text, file=sys.stderr)


def warn(message: str) -> None:
    err(f"{bold('warning:')} {message}")


def num(value: float, digits: int = 1) -> str:
    """Format a number for humans: metric, no locale separators, no trailing zeros."""
    text = f"{float(value):.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


ALIGN_CODES = {"<": "<", "r": ">", ">": ">", "^": "^"}


def print_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    aligns: Sequence[str] | None = None,
) -> None:
    """Aligned monospace table with a box-drawing rule under the header."""
    aligns = list(aligns) if aligns else ["<"] * len(headers)
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    specs = [f"{ALIGN_CODES.get(aligns[i], '<')}{widths[i]}" for i in range(len(headers))]
    head = "  ".join(f"{header:{spec}}" for header, spec in zip(headers, specs))
    out(bold(head.rstrip()))
    out(dim("  ".join("─" * width for width in widths)))
    for row in rows:
        out("  ".join(f"{cell:{spec}}" for cell, spec in zip(row, specs)).rstrip())


def print_heading(text: str) -> None:
    out(accent(bold(text)))


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Field:
    id: int
    name: str
    area_ha: float
    crop: str
    target_n_kg_per_ha: float


@dataclasses.dataclass
class SlurryBatch:
    id: int
    label: str
    source: str
    n_kg_per_m3: float
    dry_matter_pct: float | None = None
    sampled_on: str | None = None


@dataclasses.dataclass
class StorageUnit:
    id: int
    name: str
    capacity_m3: float
    level_m3: float = 0.0
    updated_at: str | None = None


@dataclasses.dataclass
class RotationEntry:
    id: int
    field_id: int
    batch_id: int
    storage_id: int
    planned_date: str
    volume_m3: float
    n_kg: float
    status: str = "planned"


@dataclasses.dataclass
class ApplicationRecord:
    id: int
    entry_id: int
    field_id: int
    batch_id: int
    applied_on: str
    volume_m3: float
    n_applied_kg: float


@dataclasses.dataclass
class State:
    schema_version: int = SCHEMA_VERSION
    units: str = "metric"
    updated_at: str = ""
    fields: list[Field] = dataclasses.field(default_factory=list)
    batches: list[SlurryBatch] = dataclasses.field(default_factory=list)
    storages: list[StorageUnit] = dataclasses.field(default_factory=list)
    plan: list[RotationEntry] = dataclasses.field(default_factory=list)
    applications: list[ApplicationRecord] = dataclasses.field(default_factory=list)

    def next_id(self, records: Sequence[Any]) -> int:
        return max((record.id for record in records), default=0) + 1

    def is_empty(self) -> bool:
        return not (self.fields or self.batches or self.storages or self.plan or self.applications)


ENTITY_TYPES: dict[str, type] = {
    "fields": Field,
    "batches": SlurryBatch,
    "storages": StorageUnit,
    "plan": RotationEntry,
    "applications": ApplicationRecord,
}


# --------------------------------------------------------------------------- #
# state file (atomic, human-readable, hand-editable)
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


@functools.lru_cache(maxsize=None)
def _field_names(cls: type) -> frozenset[str]:
    return frozenset(item.name for item in dataclasses.fields(cls))


def _record_from_dict(cls: type, raw: Any, path: Path, key: str) -> Any:
    if not isinstance(raw, dict):
        raise StateError(
            f"{path} has a '{key}' entry that is not a JSON object",
            hint=f"every item of '{key}' must be a JSON object",
        )
    known = _field_names(cls)
    try:
        return cls(**{name: value for name, value in raw.items() if name in known})
    except TypeError as exc:
        raise StateError(f"{path} has an invalid {cls.__name__} record: {exc}") from exc


def _state_number(
    value: Any,
    what: str,
    path: Path,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateError(f"{path}: {what} must be a number, got {value!r}")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise StateError(f"{path}: {what} must be a finite number, got {value!r}")
    if minimum is not None and parsed < minimum:
        raise StateError(f"{path}: {what} must be at least {num(minimum)}, got {num(parsed)}")
    if maximum is not None and parsed > maximum:
        raise StateError(f"{path}: {what} must be at most {num(maximum)}, got {num(parsed)}")
    return parsed


def _require_unique(names: list[str], kind: str, path: Path) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise StateError(f"{path}: two {kind}s are named '{name}'; names must be unique")
        seen.add(name)


def validate_state(state: State, path: Path) -> None:
    """Fail fast on a hand-edited file, with a friendly message instead of a traceback."""
    for field in state.fields:
        if not str(field.name or "").strip():
            raise StateError(f"{path}: a field record has no name")
        field.area_ha = _state_number(field.area_ha, f"field '{field.name}' area_ha", path, minimum=0.0001)
        field.target_n_kg_per_ha = _state_number(
            field.target_n_kg_per_ha, f"field '{field.name}' target_n_kg_per_ha", path, minimum=0.0001
        )
    _require_unique([field.name for field in state.fields], "field", path)

    for batch in state.batches:
        if not str(batch.label or "").strip():
            raise StateError(f"{path}: a batch record has no label")
        batch.n_kg_per_m3 = _state_number(
            batch.n_kg_per_m3, f"batch '{batch.label}' n_kg_per_m3", path, minimum=0.0001
        )
        if batch.dry_matter_pct is not None:
            batch.dry_matter_pct = _state_number(
                batch.dry_matter_pct,
                f"batch '{batch.label}' dry_matter_pct",
                path,
                minimum=0.0,
                maximum=100.0,
            )
        if batch.sampled_on:
            batch.sampled_on = state_date(batch.sampled_on).isoformat()
    _require_unique([batch.label for batch in state.batches], "batch", path)

    for storage in state.storages:
        if not str(storage.name or "").strip():
            raise StateError(f"{path}: a storage record has no name")
        storage.capacity_m3 = _state_number(
            storage.capacity_m3, f"storage '{storage.name}' capacity_m3", path, minimum=0.0001
        )
        storage.level_m3 = _state_number(
            storage.level_m3, f"storage '{storage.name}' level_m3", path, minimum=0.0
        )
    _require_unique([storage.name for storage in state.storages], "storage", path)

    for entry in state.plan:
        entry.volume_m3 = _state_number(
            entry.volume_m3, f"plan entry #{entry.id} volume_m3", path, minimum=0.0001
        )
        entry.n_kg = _state_number(entry.n_kg, f"plan entry #{entry.id} n_kg", path, minimum=0.0)
        entry.planned_date = state_date(entry.planned_date).isoformat()
        if entry.status not in ("planned", "done"):
            raise StateError(
                f"{path}: plan entry #{entry.id} has status '{entry.status}' "
                "(expected 'planned' or 'done')"
            )
        for kind, reference, records in (
            ("field", entry.field_id, state.fields),
            ("batch", entry.batch_id, state.batches),
            ("storage", entry.storage_id, state.storages),
        ):
            if not any(record.id == reference for record in records):
                raise StateError(
                    f"{path}: plan entry #{entry.id} references {kind} #{reference}, which does not exist"
                )

    for application in state.applications:
        application.volume_m3 = _state_number(
            application.volume_m3, f"application #{application.id} volume_m3", path, minimum=0.0
        )
        application.n_applied_kg = _state_number(
            application.n_applied_kg, f"application #{application.id} n_applied_kg", path, minimum=0.0
        )
        application.applied_on = state_date(application.applied_on).isoformat()
        if not any(record.id == application.field_id for record in state.fields):
            raise StateError(
                f"{path}: application #{application.id} references field "
                f"#{application.field_id}, which does not exist"
            )


def load_state(path: Path) -> State:
    """Read the state file; a missing file is an empty state, never an error."""
    if not path.exists():
        return State(updated_at=now_iso())
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"cannot read {path}: {exc}", hint="check the file permissions") from exc
    except UnicodeDecodeError as exc:
        raise StateError(
            f"{path} is not valid UTF-8 text: {exc}",
            hint="the state file must be UTF-8 JSON a human can edit",
        ) from exc
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise StateError(
            f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno})",
            hint="fix the file with a text editor, or delete it to start fresh",
        ) from exc
    if not isinstance(data, dict):
        raise StateError(f"{path} must contain a single JSON object")
    version = data.get("schema_version", SCHEMA_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        raise StateError(f"{path} has a schema_version that is not a whole number")
    if version > SCHEMA_VERSION:
        raise StateError(
            f"state file schema version {version} is newer than this tool ({SCHEMA_VERSION})",
            hint=f"upgrade {PROG} or edit schema_version in {path}",
        )
    state = State(
        schema_version=version,
        units=str(data.get("units", "metric")),
        updated_at=str(data.get("updated_at", "")),
    )
    for key, cls in ENTITY_TYPES.items():
        items = data.get(key, [])
        if not isinstance(items, list):
            raise StateError(f"{path} has a '{key}' that is not a JSON list")
        setattr(state, key, [_record_from_dict(cls, item, path, key) for item in items])
    validate_state(state, path)
    return state


def save_state(state: State, path: Path) -> None:
    """Write the whole document via temp file + os.replace: never a truncated file."""
    state.schema_version = SCHEMA_VERSION
    state.units = "metric"
    state.updated_at = now_iso()
    payload = json.dumps(dataclasses.asdict(state), indent=2, ensure_ascii=False) + "\n"
    parent = path.parent if str(path.parent) else Path(".")
    try:
        parent.mkdir(parents=True, exist_ok=True)
        handle_fd, temp_name = tempfile.mkstemp(dir=parent, prefix=f"{path.name}.", suffix=".tmp")
    except OSError as exc:
        raise StateError(f"cannot write state file {path}: {exc}") from exc
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except OSError as exc:
        # Best effort: drop the temporary file, the published state is untouched.
        try:
            Path(temp_name).unlink()
        except OSError:
            pass
        raise StateError(f"cannot write state file {path}: {exc}") from exc


def state_path(args: argparse.Namespace) -> Path:
    raw = getattr(args, "state", None)
    return Path(raw) if raw else Path(STATE_FILENAME)


# --------------------------------------------------------------------------- #
# metric validation - every message names the offending flag
# --------------------------------------------------------------------------- #
def number(raw: str | None, flag: str) -> float:
    text = (raw or "").strip()
    if "," in text:
        raise InputError(
            f"{flag} must be a number - use a dot as the decimal separator",
            hint=f"for example --{flag} 4.2",
        )
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise InputError(f"{flag} must be a number", hint=f"for example --{flag} 4.2") from None
    if not math.isfinite(value):
        raise InputError(f"{flag} must be a number")
    return value


def positive(raw: str | None, flag: str) -> float:
    value = number(raw, flag)
    if value <= 0:
        raise InputError(f"{flag} must be greater than 0")
    return value


def non_negative(raw: str | None, flag: str) -> float:
    value = number(raw, flag)
    if value < 0:
        raise InputError(f"{flag} must be 0 or greater")
    return value


def between(raw: str | None, flag: str, low: float, high: float) -> float:
    value = number(raw, flag)
    if value < low or value > high:
        raise InputError(f"{flag} must be between {num(low)} and {num(high)}")
    return value


def text_value(raw: str | None, flag: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise InputError(f"{flag} is required")
    return text


def iso_date(raw: str | None, flag: str = "date") -> str:
    text = (raw or "").strip()
    if not text:
        raise InputError(f"{flag} is required (YYYY-MM-DD)")
    if not DATE_PATTERN.match(text):
        raise InputError(f"{flag} must be YYYY-MM-DD", hint="for example --date 2026-03-14")
    try:
        date.fromisoformat(text)
    except ValueError:
        raise InputError(f"{flag} is not a real calendar date", hint="check the day and month") from None
    return text


# --------------------------------------------------------------------------- #
# shared maths (one nitrogen helper for mix, plan and status)
# --------------------------------------------------------------------------- #
def nitrogen_kg(volume_m3: float, n_kg_per_m3: float) -> float:
    """kg N delivered by ``volume_m3`` of slurry at ``n_kg_per_m3`` kg N/m3."""
    return volume_m3 * n_kg_per_m3


def required_volume_m3(target_n_kg_per_ha: float, area_ha: float, n_kg_per_m3: float) -> float:
    """Slurry volume that delivers the nitrogen target over the whole field."""
    return nitrogen_kg(target_n_kg_per_ha, area_ha) / n_kg_per_m3


def state_date(value: Any) -> date:
    """Parse a date held in the state file, with a friendly message if it is corrupt."""
    text_value_ = str(value)
    try:
        return date.fromisoformat(text_value_)
    except ValueError:
        raise StateError(
            f"the state file holds a date that is not YYYY-MM-DD: '{text_value_}'",
            hint="fix the date in the JSON file",
        ) from None


def season_of(iso: str) -> int:
    return state_date(iso).year


def today_iso() -> str:
    return date.today().isoformat()


# --------------------------------------------------------------------------- #
# record lookup (resolve names to ids at the CLI boundary)
# --------------------------------------------------------------------------- #
def _available(kind: str, names: Sequence[str], example: str) -> str:
    if names:
        return f"available {kind}s: " + ", ".join(names)
    return f"no {kind}s stored yet - add one with: {example}"


def find_field(state: State, name: str) -> Field:
    for record in state.fields:
        if record.name == name:
            return record
    raise DomainError(
        f"no field named '{name}'",
        hint=_available(
            "field",
            [record.name for record in state.fields],
            f'{PROG} field add --name "{name}" --area 4.5 --crop wheat --target-n 170',
        ),
    )


def find_batch(state: State, label: str) -> SlurryBatch:
    for record in state.batches:
        if record.label == label:
            return record
    raise DomainError(
        f"no batch named '{label}'",
        hint=_available(
            "batch",
            [record.label for record in state.batches],
            f'{PROG} batch add --label "{label}" --source cattle --n 4.2 --dm 8.5',
        ),
    )


def find_storage(state: State, name: str) -> StorageUnit:
    for record in state.storages:
        if record.name == name:
            return record
    raise DomainError(
        f"no storage named '{name}'",
        hint=_available(
            "storage",
            [record.name for record in state.storages],
            f'{PROG} storage add --name "{name}" --capacity 1200',
        ),
    )


def find_entry(state: State, raw_id: str | None) -> RotationEntry:
    text = (raw_id or "").strip()
    if text.lower().startswith("plan-"):
        text = text[5:]
    try:
        entry_id = int(text)
    except ValueError:
        raise InputError("id must be a whole number", hint="list the plan to see the entry ids") from None
    for record in state.plan:
        if record.id == entry_id:
            return record
    raise DomainError(f"no plan entry with id {entry_id}", hint=_available_plan_ids(state))


def _available_plan_ids(state: State) -> str:
    if state.plan:
        return "available ids: " + ", ".join(str(entry.id) for entry in state.plan)
    return f"the plan is empty - add an entry with: {PROG} plan add --help"


def field_by_id(state: State, field_id: int) -> Field:
    for record in state.fields:
        if record.id == field_id:
            return record
    raise DomainError(f"the state file references field #{field_id}, which no longer exists")


def batch_by_id(state: State, batch_id: int) -> SlurryBatch:
    for record in state.batches:
        if record.id == batch_id:
            return record
    raise DomainError(f"the state file references batch #{batch_id}, which no longer exists")


def storage_by_id(state: State, storage_id: int) -> StorageUnit:
    for record in state.storages:
        if record.id == storage_id:
            return record
    raise DomainError(f"the state file references storage #{storage_id}, which no longer exists")


# --------------------------------------------------------------------------- #
# season accounting
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class FieldSeason:
    target_kg: float
    planned_kg: float
    applied_kg: float
    remaining_kg: float


def field_season(state: State, field: Field, season: int) -> FieldSeason:
    """Target, still-planned, applied and remaining nitrogen for one field and season."""
    applied = sum(
        record.n_applied_kg
        for record in state.applications
        if record.field_id == field.id and season_of(record.applied_on) == season
    )
    planned = sum(
        entry.n_kg
        for entry in state.plan
        if entry.field_id == field.id
        and entry.status == "planned"
        and season_of(entry.planned_date) == season
    )
    target = nitrogen_kg(field.target_n_kg_per_ha, field.area_ha)
    return FieldSeason(target, planned, applied, target - applied - planned)


def storage_drawdown(state: State, storage: StorageUnit) -> tuple[float, float, float]:
    """Planned volume, day span and planned m3/day for the spreads left on a unit."""
    entries = [entry for entry in state.plan if entry.storage_id == storage.id and entry.status == "planned"]
    volume = sum(entry.volume_m3 for entry in entries)
    if not entries:
        return 0.0, 0.0, 0.0
    dates = sorted(entry.planned_date for entry in entries)
    span = (state_date(dates[-1]) - state_date(dates[0])).days + 1
    return volume, float(span), volume / span


def percent_full(level_m3: float, capacity_m3: float) -> float:
    return 0.0 if capacity_m3 <= 0 else level_m3 / capacity_m3 * 100.0


def full_bar(level_m3: float, capacity_m3: float, width: int = 10) -> str:
    """Percent-full bar; the percentage is always printed, never signalled by colour alone."""
    percent = percent_full(level_m3, capacity_m3)
    filled = max(0, min(width, int(round(percent / 100.0 * width))))
    return f"[{'#' * filled}{'-' * (width - filled)}] {round(percent)}%"


def days_of_spreading(state: State, storage: StorageUnit) -> int | None:
    _volume, _span, rate = storage_drawdown(state, storage)
    if rate <= 0:
        return None
    return math.ceil(storage.level_m3 / rate)


def storage_days_label(state: State, storage: StorageUnit) -> str:
    days = days_of_spreading(state, storage)
    if days is None:
        return "no planned spreads"
    _volume, _span, rate = storage_drawdown(state, storage)
    return f"{days} days at {num(rate)} m3/day"


# --------------------------------------------------------------------------- #
# mix - the nitrogen ratio calculator
# --------------------------------------------------------------------------- #
def mix_inputs(args: argparse.Namespace, state: State) -> dict[str, Any]:
    notes: list[str] = []
    # Every flag the operator actually typed is validated first: a typo must never
    # be silently ignored just because a stored record would have supplied the value.
    inline_n = positive(args.n, "n") if args.n else None
    inline_area = positive(args.area, "area") if args.area else None
    inline_target = positive(args.target, "target") if args.target else None
    planned_date = iso_date(args.date, "date") if args.date else None
    field = find_field(state, args.field) if args.field else None
    batch = find_batch(state, args.batch) if args.batch else None

    if batch is not None:
        n_kg_per_m3 = batch.n_kg_per_m3
        if inline_n is not None:
            notes.append(f"batch '{batch.label}' nitrogen {num(batch.n_kg_per_m3)} kg N/m3 overrode --n")
    elif inline_n is not None:
        n_kg_per_m3 = inline_n
    else:
        raise InputError("nitrogen is required (pass --n or --batch)")

    if field is not None:
        area_ha = field.area_ha
        target_n_kg_per_ha = field.target_n_kg_per_ha
        if inline_area is not None:
            notes.append(f"field '{field.name}' area {num(field.area_ha)} ha overrode --area")
        if inline_target is not None:
            notes.append(
                f"field '{field.name}' target {num(field.target_n_kg_per_ha)} kg N/ha overrode --target"
            )
    else:
        if inline_area is None:
            raise InputError("area is required (pass --area or --field)")
        if inline_target is None:
            raise InputError("target is required (pass --target or --field)")
        area_ha = inline_area
        target_n_kg_per_ha = inline_target

    target_kg = nitrogen_kg(target_n_kg_per_ha, area_ha)
    volume_m3 = round(required_volume_m3(target_n_kg_per_ha, area_ha, n_kg_per_m3), 1)
    delivered_kg = nitrogen_kg(volume_m3, n_kg_per_m3)
    season = season_of(planned_date) if planned_date else date.today().year
    return {
        "field": field,
        "batch": batch,
        "notes": notes,
        "n_kg_per_m3": n_kg_per_m3,
        "area_ha": area_ha,
        "target_n_kg_per_ha": target_n_kg_per_ha,
        "target_kg": target_kg,
        "volume_m3": volume_m3,
        "delivered_kg": delivered_kg,
        "per_hectare_m3": volume_m3 / area_ha,
        "delivered_per_ha": delivered_kg / area_ha,
        "balance_kg": delivered_kg - target_kg,
        "season": season,
    }


def balance_line(balance_kg: float) -> str:
    if abs(balance_kg) < 0.05:
        return "on target"
    if balance_kg > 0:
        return f"surplus {num(balance_kg)} kg N"
    return f"shortfall {num(-balance_kg)} kg N"


def cmd_mix(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    plan = mix_inputs(args, state)
    field = plan["field"]
    batch = plan["batch"]

    remaining_after: float | None = None
    if field is not None:
        remaining_after = field_season(state, field, plan["season"]).remaining_kg - plan["delivered_kg"]

    if getattr(args, "json", False):
        out(json.dumps(_mix_json(plan, remaining_after), indent=2, ensure_ascii=False))
        return 0

    print_heading("Slurry mix - nitrogen target")
    source_n = f"batch '{batch.label}'" if batch else "inline --n"
    source_area = f"field '{field.name}'" if field else "inline --area"
    source_target = f"field '{field.name}'" if field else "inline --target"
    print_table(
        ["INPUT", "VALUE", "SOURCE"],
        [
            ["nitrogen content", f"{num(plan['n_kg_per_m3'])} kg N/m3", source_n],
            ["field area", f"{num(plan['area_ha'])} ha", source_area],
            ["target rate", f"{num(plan['target_n_kg_per_ha'])} kg N/ha", source_target],
            ["target nitrogen", f"{num(plan['target_kg'])} kg N", "rate x area"],
        ],
    )
    out()
    print_heading("Result")
    print_table(
        ["QUANTITY", "VALUE"],
        [
            ["required slurry", f"{num(plan['volume_m3'])} m3"],
            ["per-hectare rate", f"{num(plan['per_hectare_m3'])} m3/ha"],
            [
                "nitrogen delivered",
                f"{num(plan['delivered_kg'])} kg N ({num(plan['delivered_per_ha'])} kg N/ha)",
            ],
            [
                "applied vs target",
                f"target {num(plan['target_n_kg_per_ha'])} kg N/ha - {balance_line(plan['balance_kg'])}",
            ],
        ],
    )
    if field is not None and remaining_after is not None:
        out()
        label = "over target" if remaining_after < 0 else "in hand"
        out(
            f"remaining season budget for {accent(field.name)} after this spread: "
            f"{num(remaining_after)} kg N ({label})"
        )
        if remaining_after < 0:
            warn(
                f"{field.name} exceeds its remaining season budget by {num(-remaining_after)} kg N "
                "with this spread"
            )
    for note in plan["notes"]:
        out(f"note: {note}")
    if batch is not None:
        out(f"note: nitrogen taken from stored batch '{batch.label}' ({num(batch.n_kg_per_m3)} kg N/m3)")
    return 0


def _mix_json(plan: dict[str, Any], remaining_after: float | None) -> dict[str, Any]:
    return {
        "field": plan["field"].name if plan["field"] else None,
        "batch": plan["batch"].label if plan["batch"] else None,
        "n_kg_per_m3": round(plan["n_kg_per_m3"], 3),
        "area_ha": round(plan["area_ha"], 3),
        "target_n_kg_per_ha": round(plan["target_n_kg_per_ha"], 2),
        "target_n_kg": round(plan["target_kg"], 2),
        "required_volume_m3": round(plan["volume_m3"], 2),
        "per_hectare_volume_m3": round(plan["per_hectare_m3"], 2),
        "delivered_n_kg": round(plan["delivered_kg"], 2),
        "delivered_n_kg_per_ha": round(plan["delivered_per_ha"], 2),
        "balance_n_kg": round(plan["balance_kg"], 2),
        "remaining_season_n_kg": None if remaining_after is None else round(remaining_after, 2),
        "units": "metric",
        "notes": plan["notes"],
    }


# --------------------------------------------------------------------------- #
# field register
# --------------------------------------------------------------------------- #
def cmd_field_add(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    name = text_value(args.name, "name")
    area_ha = positive(args.area, "area")
    crop = text_value(args.crop, "crop")
    target = positive(args.target_n, "target-n")
    if any(record.name == name for record in state.fields):
        raise DomainError(f"field '{name}' already exists", hint="use a different --name")
    record = Field(
        id=state.next_id(state.fields),
        name=name,
        area_ha=area_ha,
        crop=crop,
        target_n_kg_per_ha=target,
    )
    state.fields.append(record)
    save_state(state, path)
    out(
        f"added field {accent(record.name)} (#{record.id}): {num(area_ha)} ha of {crop}, "
        f"target {num(target)} kg N/ha ({num(nitrogen_kg(target, area_ha))} kg N this season)"
    )
    return 0


def cmd_field_list(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    if getattr(args, "json", False):
        out(json.dumps({"fields": [_field_json(record) for record in state.fields]}, indent=2))
        return 0
    if not state.fields:
        out("no fields yet - register one with:")
        out(f'  {PROG} field add --name "North 12" --area 4.5 --crop wheat --target-n 170')
        return 0
    print_heading(f"Fields ({len(state.fields)})")
    print_table(
        ["ID", "NAME", "AREA (ha)", "CROP", "TARGET (kg N/ha)"],
        [
            [
                str(record.id),
                record.name,
                num(record.area_ha),
                record.crop,
                num(record.target_n_kg_per_ha),
            ]
            for record in state.fields
        ],
        aligns=["r", "<", "r", "<", "r"],
    )
    return 0


def _field_json(field: Field) -> dict[str, Any]:
    return {
        "id": field.id,
        "name": field.name,
        "area_ha": round(field.area_ha, 3),
        "crop": field.crop,
        "target_n_kg_per_ha": round(field.target_n_kg_per_ha, 2),
    }


# --------------------------------------------------------------------------- #
# batch register
# --------------------------------------------------------------------------- #
def cmd_batch_add(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    label = text_value(args.label, "label")
    source = text_value(args.source, "source").lower()
    if source not in SOURCES:
        raise InputError(f"source must be one of {', '.join(SOURCES)}", hint=f"got '{source}'")
    n_kg_per_m3 = positive(args.n, "n")
    dry_matter = between(args.dm, "dm", 0, 100) if args.dm else None
    sampled_on = iso_date(args.sampled, "sampled") if args.sampled else None
    if any(record.label == label for record in state.batches):
        raise DomainError(f"batch '{label}' already exists", hint="use a different --label")
    record = SlurryBatch(
        id=state.next_id(state.batches),
        label=label,
        source=source,
        n_kg_per_m3=n_kg_per_m3,
        dry_matter_pct=dry_matter,
        sampled_on=sampled_on,
    )
    state.batches.append(record)
    save_state(state, path)
    detail = f"{num(n_kg_per_m3)} kg N/m3"
    if dry_matter is not None:
        detail += f", {num(dry_matter)}% dry matter"
    out(f"added batch {accent(record.label)} (#{record.id}): {source} slurry, {detail}")
    return 0


def cmd_batch_list(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    if getattr(args, "json", False):
        out(json.dumps({"batches": [_batch_json(record) for record in state.batches]}, indent=2))
        return 0
    if not state.batches:
        out("no slurry batches yet - record one with:")
        out(f'  {PROG} batch add --label "Pit A March" --source cattle --n 4.2 --dm 8.5')
        return 0
    print_heading(f"Slurry batches ({len(state.batches)})")
    print_table(
        ["ID", "LABEL", "SOURCE", "N (kg N/m3)", "DRY MATTER (%)", "SAMPLED"],
        [
            [
                str(record.id),
                record.label,
                record.source,
                num(record.n_kg_per_m3),
                "-" if record.dry_matter_pct is None else num(record.dry_matter_pct),
                record.sampled_on or "-",
            ]
            for record in state.batches
        ],
        aligns=["r", "<", "<", "r", "r", "<"],
    )
    return 0


def _batch_json(batch: SlurryBatch) -> dict[str, Any]:
    return {
        "id": batch.id,
        "label": batch.label,
        "source": batch.source,
        "n_kg_per_m3": round(batch.n_kg_per_m3, 3),
        "dry_matter_pct": None if batch.dry_matter_pct is None else round(batch.dry_matter_pct, 1),
        "sampled_on": batch.sampled_on,
    }


# --------------------------------------------------------------------------- #
# storage register
# --------------------------------------------------------------------------- #
def cmd_storage_add(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    name = text_value(args.name, "name")
    capacity = positive(args.capacity, "capacity")
    if any(record.name == name for record in state.storages):
        raise DomainError(f"storage '{name}' already exists", hint="use a different --name")
    record = StorageUnit(
        id=state.next_id(state.storages),
        name=name,
        capacity_m3=capacity,
        level_m3=0.0,
        updated_at=now_iso(),
    )
    state.storages.append(record)
    save_state(state, path)
    out(f"added storage {accent(record.name)} (#{record.id}): capacity {num(capacity)} m3, level 0 m3")
    return 0


def cmd_storage_set(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    name = text_value(args.name, "name")
    level = non_negative(args.level, "level")
    record = find_storage(state, name)
    record.level_m3 = round(level, 1)
    record.updated_at = now_iso()
    save_state(state, path)
    percent = round(percent_full(record.level_m3, record.capacity_m3))
    out(
        f"{record.name}: level {num(record.level_m3)} m3 of {num(record.capacity_m3)} m3 "
        f"({percent}% full, {num(record.capacity_m3 - record.level_m3)} m3 free)"
    )
    if record.level_m3 > record.capacity_m3:
        over = record.level_m3 - record.capacity_m3
        warn(f"{record.name} is over capacity by {num(over)} m3 ({percent}%) - plan spreads to draw it down")
    elif record.level_m3 == record.capacity_m3:
        warn(f"{record.name} is exactly at capacity ({num(record.capacity_m3)} m3) - no free space left")
    return 0


STORAGE_HEADERS = ["ID", "NAME", "LEVEL", "CAPACITY", "FREE", "PERCENT FULL", "FLAG"]
STORAGE_ALIGNS = ["r", "<", "r", "r", "r", "<", "<"]


def storage_rows(state: State) -> list[list[str]]:
    """One row per unit, every number carrying its unit."""
    rows = []
    for record in state.storages:
        if record.level_m3 > record.capacity_m3:
            flag = "over capacity"
        elif record.level_m3 == record.capacity_m3:
            flag = "at capacity"
        else:
            flag = ""
        rows.append(
            [
                str(record.id),
                record.name,
                f"{num(record.level_m3)} m3",
                f"{num(record.capacity_m3)} m3",
                f"{num(record.capacity_m3 - record.level_m3)} m3",
                full_bar(record.level_m3, record.capacity_m3),
                flag,
            ]
        )
    return rows


def print_storage_table(state: State) -> None:
    print_table(STORAGE_HEADERS, storage_rows(state), aligns=STORAGE_ALIGNS)


def cmd_storage_list(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    if getattr(args, "json", False):
        out(json.dumps({"storages": [_storage_json(state, record) for record in state.storages]}, indent=2))
        return 0
    if not state.storages:
        out("no storage units yet - define one with:")
        out(f'  {PROG} storage add --name "Lagoon 1" --capacity 1200')
        return 0
    print_heading(f"Storage units ({len(state.storages)})")
    print_storage_table(state)
    for record in state.storages:
        if record.level_m3 > record.capacity_m3:
            percent = round(percent_full(record.level_m3, record.capacity_m3))
            warn(f"{record.name} is over capacity by {num(record.level_m3 - record.capacity_m3)} m3 ({percent}%)")
    return 0


def _storage_json(state: State, storage: StorageUnit) -> dict[str, Any]:
    volume, span, rate = storage_drawdown(state, storage)
    return {
        "id": storage.id,
        "name": storage.name,
        "capacity_m3": round(storage.capacity_m3, 2),
        "level_m3": round(storage.level_m3, 2),
        "free_m3": round(storage.capacity_m3 - storage.level_m3, 2),
        "percent_full": round(percent_full(storage.level_m3, storage.capacity_m3), 1),
        "at_capacity": storage.level_m3 >= storage.capacity_m3,
        "over_capacity": storage.level_m3 > storage.capacity_m3,
        "planned_drawdown_m3": round(volume, 2),
        "planned_span_days": int(span),
        "planned_rate_m3_per_day": round(rate, 2),
        "days_of_spreading_remaining": days_of_spreading(state, storage),
        "updated_at": storage.updated_at,
    }


# --------------------------------------------------------------------------- #
# rotation planner
# --------------------------------------------------------------------------- #
def cmd_plan_add(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    field = find_field(state, text_value(args.field, "field"))
    batch = find_batch(state, text_value(args.batch, "batch"))
    storage = find_storage(state, text_value(args.storage, "storage"))
    planned_date = iso_date(args.date, "date")

    calculated = not args.volume
    if calculated:
        volume = required_volume_m3(field.target_n_kg_per_ha, field.area_ha, batch.n_kg_per_m3)
    else:
        volume = positive(args.volume, "volume")
    entry = RotationEntry(
        id=state.next_id(state.plan),
        field_id=field.id,
        batch_id=batch.id,
        storage_id=storage.id,
        planned_date=planned_date,
        volume_m3=round(volume, 1),
        n_kg=round(nitrogen_kg(volume, batch.n_kg_per_m3), 1),
        status="planned",
    )
    season_state = field_season(state, field, season_of(planned_date))
    over_budget = entry.n_kg - season_state.remaining_kg

    state.plan.append(entry)
    save_state(state, path)

    origin = "calculated from the field's nitrogen target" if calculated else "from --volume"
    out(
        f"planned entry #{entry.id}: {num(entry.volume_m3)} m3 ({origin}) of {batch.label} onto "
        f"{field.name} from {storage.name} on {entry.planned_date} - {num(entry.n_kg)} kg N"
    )
    if over_budget > 0:
        warn(
            f"{field.name} exceeds its remaining {num(season_state.remaining_kg)} kg N this season "
            f"by {num(over_budget)} kg N"
        )
    return 0


def sorted_plan(state: State) -> list[RotationEntry]:
    return sorted(state.plan, key=lambda entry: (entry.planned_date, entry.id))


def plan_rows(state: State, entries: Sequence[RotationEntry] | None = None) -> list[list[str]]:
    rows = []
    for entry in sorted_plan(state) if entries is None else entries:
        rows.append(
            [
                str(entry.id),
                entry.planned_date,
                field_by_id(state, entry.field_id).name,
                batch_by_id(state, entry.batch_id).label,
                storage_by_id(state, entry.storage_id).name,
                num(entry.volume_m3),
                num(entry.n_kg),
                entry.status,
            ]
        )
    return rows


def cmd_plan_list(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    every_entry = sorted_plan(state)
    planned = [entry for entry in every_entry if entry.status == "planned"]
    completed = len(every_entry) - len(planned)
    show_all = bool(getattr(args, "all", False))
    shown = every_entry if show_all else planned
    if getattr(args, "json", False):
        out(
            json.dumps(
                {
                    "plan": [_entry_json(state, entry) for entry in shown],
                    "planned": len(planned),
                    "completed": completed,
                },
                indent=2,
            )
        )
        return 0
    if not shown:
        if every_entry:
            out(f"no planned spreads left - {completed} completed spread(s) on record.")
            out(f"  list them with: {PROG} plan --all")
            return 0
        out("no planned spreads yet - add one with:")
        out(
            f'  {PROG} plan add --field "North 12" --batch "Pit A March" '
            f'--storage "Lagoon 1" --date {today_iso()}'
        )
        return 0
    scope = "all entries" if show_all else "planned"
    print_heading(f"Rotation plan ({len(shown)} {scope})")
    print_table(
        ["ID", "DATE", "FIELD", "BATCH", "STORAGE", "VOLUME (m3)", "N (kg N)", "STATUS"],
        plan_rows(state, shown),
        aligns=["r", "<", "<", "<", "<", "r", "r", "<"],
    )
    out()
    out(
        f"total planned {num(sum(entry.volume_m3 for entry in planned))} m3 / "
        f"{num(sum(entry.n_kg for entry in planned))} kg N over {len(planned)} entry(s); "
        f"{completed} completed"
    )
    if completed and not show_all:
        out(f"  {completed} completed spread(s) hidden - pass --all to list them")
    for record in state.storages:
        if record.level_m3 > record.capacity_m3:
            warn(
                f"{record.name} is over capacity by "
                f"{num(record.level_m3 - record.capacity_m3)} m3 "
                f"({round(percent_full(record.level_m3, record.capacity_m3))}%)"
            )
    return 0


def _entry_json(state: State, entry: RotationEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "planned_date": entry.planned_date,
        "field": field_by_id(state, entry.field_id).name,
        "batch": batch_by_id(state, entry.batch_id).label,
        "storage": storage_by_id(state, entry.storage_id).name,
        "volume_m3": round(entry.volume_m3, 2),
        "n_kg": round(entry.n_kg, 2),
        "status": entry.status,
    }


def cmd_plan_remove(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    entry = find_entry(state, args.id)
    if entry.status == "done":
        raise DomainError(
            f"plan entry {entry.id} is already done and cannot be removed",
            hint="completed spreads stay as application records",
        )
    field = field_by_id(state, entry.field_id)
    batch = batch_by_id(state, entry.batch_id)
    state.plan.remove(entry)
    save_state(state, path)
    out(
        f"removed plan entry #{entry.id}: {num(entry.volume_m3)} m3 of {batch.label} planned for "
        f"{field.name} on {entry.planned_date}"
    )
    return 0


def cmd_plan_done(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    entry = find_entry(state, args.id)
    if entry.status == "done":
        raise DomainError(f"plan entry {entry.id} is already done")
    field = field_by_id(state, entry.field_id)
    batch = batch_by_id(state, entry.batch_id)
    storage = storage_by_id(state, entry.storage_id)

    application = ApplicationRecord(
        id=state.next_id(state.applications),
        entry_id=entry.id,
        field_id=entry.field_id,
        batch_id=entry.batch_id,
        applied_on=entry.planned_date,
        volume_m3=entry.volume_m3,
        n_applied_kg=entry.n_kg,
    )
    shortage = 0.0
    if storage.level_m3 >= entry.volume_m3:
        storage.level_m3 = round(storage.level_m3 - entry.volume_m3, 1)
    else:
        shortage = entry.volume_m3 - storage.level_m3
        storage.level_m3 = 0.0
    storage.updated_at = now_iso()
    state.applications.append(application)
    entry.status = "done"
    save_state(state, path)

    percent = round(percent_full(storage.level_m3, storage.capacity_m3))
    out(
        f"entry #{entry.id} done: {num(entry.volume_m3)} m3 / {num(entry.n_kg)} kg N of {batch.label} "
        f"applied to {field.name} on {entry.planned_date}"
    )
    out(
        f"{storage.name} is now {num(storage.level_m3)} m3 of {num(storage.capacity_m3)} m3 "
        f"({percent}% full, {num(storage.capacity_m3 - storage.level_m3)} m3 free)"
    )
    if shortage > 0:
        warn(
            f"{storage.name} was short by {num(shortage)} m3 for this spread; level set to 0 m3"
        )
    season_state = field_season(state, field, season_of(entry.planned_date))
    out(
        f"{field.name} season {season_of(entry.planned_date)}: {num(season_state.applied_kg)} kg N applied "
        f"against a {num(season_state.target_kg)} kg N target"
    )
    return 0


# --------------------------------------------------------------------------- #
# status summary
# --------------------------------------------------------------------------- #
def cmd_status(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    season = date.today().year
    if getattr(args, "json", False):
        out(json.dumps(_status_json(state, season), indent=2, ensure_ascii=False))
        return 0
    if state.is_empty():
        print_heading("Slurrymix is empty - first run checklist")
        out("1. register a field:")
        out(f'     {PROG} field add --name "North 12" --area 4.5 --crop wheat --target-n 170')
        out("2. record the slurry you have analysed:")
        out(f'     {PROG} batch add --label "Pit A March" --source cattle --n 4.2 --dm 8.5')
        out("3. define where it is stored:")
        out(f'     {PROG} storage add --name "Lagoon 1" --capacity 1200')
        out("then plan a spread:")
        out(
            f'     {PROG} plan add --field "North 12" --batch "Pit A March" '
            f'--storage "Lagoon 1" --date {today_iso()}'
        )
        return 0

    planned = [entry for entry in state.plan if entry.status == "planned"]
    done = [entry for entry in state.plan if entry.status == "done"]
    print_heading(f"Slurrymix status - season {season}")
    print_table(
        ["RECORDS", "COUNT"],
        [
            ["fields", str(len(state.fields))],
            ["batches", str(len(state.batches))],
            ["storages", str(len(state.storages))],
            ["plan entries", f"{len(state.plan)} ({len(planned)} planned, {len(done)} done)"],
            ["applications", str(len(state.applications))],
        ],
        aligns=["<", "r"],
    )
    out()
    print_table(
        ["SEASON TOTALS", "VALUE"],
        [
            ["planned volume", f"{num(sum(e.volume_m3 for e in planned))} m3"],
            ["planned nitrogen", f"{num(sum(e.n_kg for e in planned))} kg N"],
            ["applied volume", f"{num(sum(a.volume_m3 for a in state.applications))} m3"],
            ["applied nitrogen", f"{num(sum(a.n_applied_kg for a in state.applications))} kg N"],
        ],
        aligns=["<", "r"],
    )
    if state.fields:
        out()
        print_heading(f"Nitrogen per field (season {season})")
        field_rows = []
        for field in state.fields:
            totals = field_season(state, field, season)
            percent = 0.0 if totals.target_kg <= 0 else totals.applied_kg / totals.target_kg * 100
            field_rows.append(
                [
                    field.name,
                    num(totals.target_kg),
                    num(totals.planned_kg),
                    num(totals.applied_kg),
                    num(totals.remaining_kg),
                    f"{round(percent)}%",
                ]
            )
        print_table(
            [
                "FIELD",
                "TARGET (kg N)",
                "PLANNED (kg N)",
                "APPLIED (kg N)",
                "REMAINING (kg N)",
                "APPLIED OF TARGET",
            ],
            field_rows,
            aligns=["<", "r", "r", "r", "r", "r"],
        )
    if state.storages:
        out()
        print_heading("Storage levels")
        print_table(
            ["NAME", "LEVEL (m3)", "CAPACITY (m3)", "FREE (m3)", "PERCENT FULL", "DAYS OF SPREADING"],
            [
                [
                    record.name,
                    num(record.level_m3),
                    num(record.capacity_m3),
                    num(record.capacity_m3 - record.level_m3),
                    full_bar(record.level_m3, record.capacity_m3),
                    storage_days_label(state, record),
                ]
                for record in state.storages
            ],
            aligns=["<", "r", "r", "r", "<", "<"],
        )
        for record in state.storages:
            if record.level_m3 > record.capacity_m3:
                warn(
                    f"{record.name} is over capacity by "
                    f"{num(record.level_m3 - record.capacity_m3)} m3 "
                    f"({round(percent_full(record.level_m3, record.capacity_m3))}%)"
                )
    return 0


def _status_json(state: State, season: int) -> dict[str, Any]:
    planned = [entry for entry in state.plan if entry.status == "planned"]
    return {
        "season": season,
        "units": "metric",
        "fields": [
            {
                "id": field.id,
                "name": field.name,
                "area_ha": round(field.area_ha, 3),
                "crop": field.crop,
                "target_n_kg_per_ha": round(field.target_n_kg_per_ha, 2),
                "season_target_n_kg": round(field_season(state, field, season).target_kg, 2),
                "planned_n_kg": round(field_season(state, field, season).planned_kg, 2),
                "applied_n_kg": round(field_season(state, field, season).applied_kg, 2),
                "remaining_n_kg": round(field_season(state, field, season).remaining_kg, 2),
            }
            for field in state.fields
        ],
        "batches": [_batch_json(batch) for batch in state.batches],
        "storages": [_storage_json(state, storage) for storage in state.storages],
        "plan": [_entry_json(state, entry) for entry in sorted_plan(state)],
        "totals": {
            "fields": len(state.fields),
            "batches": len(state.batches),
            "storages": len(state.storages),
            "plan_entries": len(state.plan),
            "planned_entries": len(planned),
            "done_entries": len(state.plan) - len(planned),
            "applications": len(state.applications),
            "planned_volume_m3": round(sum(entry.volume_m3 for entry in planned), 2),
            "planned_n_kg": round(sum(entry.n_kg for entry in planned), 2),
            "applied_volume_m3": round(sum(app.volume_m3 for app in state.applications), 2),
            "applied_n_kg": round(sum(app.n_applied_kg for app in state.applications), 2),
        },
    }


# --------------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------------- #
PLAN_HEADER = ["id", "planned_date", "field", "batch", "storage", "volume_m3", "n_kg", "status"]
STORAGE_HEADER = ["id", "name", "capacity_m3", "level_m3", "free_m3", "percent_full"]


def _write_csv(path: Path, header: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    try:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
    except OSError as exc:
        raise StateError(f"cannot write {path}: {exc}") from exc


def cmd_export(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    target_dir = Path(args.out) if args.out else Path(".")
    plan_path = target_dir / "plan.csv"
    storage_path = target_dir / "storage.csv"
    if not args.force:
        for candidate in (plan_path, storage_path):
            if candidate.exists():
                raise DomainError(
                    f"{candidate} exists; pass --force to overwrite",
                    hint="--force replaces that file only",
                )
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StateError(f"cannot create {target_dir}: {exc}") from exc

    plan_data = plan_rows(state)
    storage_data = [
        [
            record.id,
            record.name,
            num(record.capacity_m3),
            num(record.level_m3),
            num(record.capacity_m3 - record.level_m3),
            round(percent_full(record.level_m3, record.capacity_m3)),
        ]
        for record in state.storages
    ]
    _write_csv(plan_path, PLAN_HEADER, plan_data)
    _write_csv(storage_path, STORAGE_HEADER, storage_data)

    if getattr(args, "json", False):
        out(
            json.dumps(
                {
                    "plan_csv": str(plan_path),
                    "plan_rows": len(plan_data),
                    "storage_csv": str(storage_path),
                    "storage_rows": len(storage_data),
                },
                indent=2,
            )
        )
        return 0
    out(f"wrote {plan_path} ({len(plan_data)} row(s))")
    out(f"wrote {storage_path} ({len(storage_data)} row(s))")
    if not state.plan and not state.storages:
        out("both files hold only their header row - add records and export again")
    return 0


# --------------------------------------------------------------------------- #
# guided wizard - writes state once, at the end
# --------------------------------------------------------------------------- #
def _ask(prompt: str, default: str) -> str:
    try:
        answer = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        raise WizardAbort(
            "input ended - nothing was written",
            hint=f"{PROG} wizard needs an interactive terminal",
        ) from None
    return answer or default


def _ask_validated(prompt: str, default: str, validator: Callable[[str], Any]) -> Any:
    while True:
        raw = _ask(prompt, default)
        try:
            return validator(raw)
        except CliError as exc:
            err(f"{bold('error:')} {exc.message}")
            if exc.hint:
                err(f"hint: {exc.hint}")


def _wizard_source(value: str) -> str:
    source = text_value(value, "source").lower()
    if source not in SOURCES:
        raise InputError(f"source must be one of {', '.join(SOURCES)}", hint=f"got '{source}'")
    return source


def cmd_wizard(args: argparse.Namespace, path: Path) -> int:
    state = load_state(path)
    print_heading("Slurrymix wizard - record a batch, plan a spread, check storage")
    out("press Enter to accept the value in brackets; Ctrl-C exits without writing anything")
    out()

    def unique_label(value: str) -> str:
        label = text_value(value, "label")
        if any(record.label == label for record in state.batches):
            raise DomainError(f"batch '{label}' already exists", hint="pick another label")
        return label

    label = _ask_validated("Batch label", "Pit B April", unique_label)
    source = _ask_validated("Slurry source (cattle|pig|poultry|other)", "cattle", _wizard_source)
    n_kg_per_m3 = _ask_validated("Nitrogen content kg N/m3", "4.2", lambda value: positive(value, "n"))
    dry_matter = _ask_validated(
        "Dry matter % (0-100, blank to skip)",
        "8.5",
        lambda value: between(value, "dm", 0, 100),
    )

    batch = SlurryBatch(
        id=state.next_id(state.batches),
        label=label,
        source=source,
        n_kg_per_m3=n_kg_per_m3,
        dry_matter_pct=dry_matter,
        sampled_on=today_iso(),
    )

    entry: RotationEntry | None = None
    if state.fields and state.storages:
        field = _ask_validated(
            "Field to spread on (" + ", ".join(record.name for record in state.fields) + ")",
            state.fields[0].name,
            lambda value: find_field(state, value).name,
        )
        storage = _ask_validated(
            "Storage unit (" + ", ".join(record.name for record in state.storages) + ")",
            state.storages[0].name,
            lambda value: find_storage(state, value).name,
        )
        planned_date = _ask_validated("Planned date", today_iso(), lambda value: iso_date(value, "date"))
        planned_field = find_field(state, field)
        default_volume = num(
            round(required_volume_m3(planned_field.target_n_kg_per_ha, planned_field.area_ha, n_kg_per_m3), 1)
        )
        volume = _ask_validated("Volume m3", default_volume, lambda value: positive(value, "volume"))
        entry = RotationEntry(
            id=state.next_id(state.plan),
            field_id=planned_field.id,
            batch_id=batch.id,
            storage_id=find_storage(state, storage).id,
            planned_date=planned_date,
            volume_m3=round(volume, 1),
            n_kg=round(nitrogen_kg(volume, n_kg_per_m3), 1),
            status="planned",
        )
    else:
        out()
        out("skipping the spread step - it needs at least one field and one storage unit:")
        out(f'  {PROG} field add --name "North 12" --area 4.5 --crop wheat --target-n 170')
        out(f'  {PROG} storage add --name "Lagoon 1" --capacity 1200')

    state.batches.append(batch)
    if entry is not None:
        state.plan.append(entry)
    save_state(state, path)

    out()
    out(f"saved batch '{batch.label}' ({num(batch.n_kg_per_m3)} kg N/m3)")
    if entry is not None:
        out(
            f"saved plan entry #{entry.id}: {num(entry.volume_m3)} m3 / {num(entry.n_kg)} kg N "
            f"on {entry.planned_date}"
        )
    out()
    if state.storages:
        print_heading("Storage levels")
        print_storage_table(state)
    else:
        out("no storage units yet - define one with:")
        out(f'  {PROG} storage add --name "Lagoon 1" --capacity 1200')
    return 0


# --------------------------------------------------------------------------- #
# CLI shell
# --------------------------------------------------------------------------- #
class CliParser(argparse.ArgumentParser):
    """Argument parser that raises CliError instead of printing usage and exiting."""

    role = "command"

    def error(self, message: str) -> None:  # type: ignore[override]
        invalid = re.search(r"invalid choice: '([^']+)'", message)
        if invalid:
            raise InputError(
                f"unknown {self.role} '{invalid.group(1)}'",
                hint=f"run '{PROG} --help' to see the available commands",
            )
        cleaned = message.replace("unrecognized arguments:", "unknown flag:").strip()
        raise InputError(cleaned, hint=f"run '{PROG} <command> --help' for the accepted flags")


HELP_EPILOG = f"""\
examples:
  {PROG} mix --n 4.2 --area 3 --target 170
  {PROG} mix --field "North 12" --batch "Pit A March"
  {PROG} field add --name "North 12" --area 4.5 --crop wheat --target-n 170
  {PROG} batch add --label "Pit A March" --source cattle --n 4.2 --dm 8.5
  {PROG} storage add --name "Lagoon 1" --capacity 1200
  {PROG} plan add --field "North 12" --batch "Pit A March" --storage "Lagoon 1" --date 2026-03-14
  {PROG} plan done --id 1
  {PROG} status --json

units are metric everywhere: m3, hectares (ha), kg N and kg N/m3.
state lives in ./{STATE_FILENAME} (override with --state PATH) and is written atomically.
exit codes: 0 success, 1 domain error, 2 bad input, 130 interrupted.
"""


def _global_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--state",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help=f"state file to use (default: ./{STATE_FILENAME})",
    )
    parent.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print one JSON object on stdout (read commands)",
    )
    parent.add_argument(
        "--no-color",
        action="store_true",
        default=argparse.SUPPRESS,
        help="disable ANSI colour (also honours the NO_COLOR environment variable)",
    )
    return parent


def build_parser() -> CliParser:
    parser = CliParser(
        prog=PROG,
        description=(
            "Slurrymix - manure slurry ratio calculator, spread rotation planner "
            "and storage capacity tracker."
        ),
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_global_parent()],
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {VERSION}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", title="commands")

    mix = sub.add_parser(
        "mix",
        parents=[_global_parent()],
        help="calculate the slurry volume that delivers a nitrogen target",
        description="Compute the slurry volume and per-hectare rate that deliver a nitrogen target.",
    )
    mix.add_argument("--n", metavar="KG_N_PER_M3", help="nitrogen content of the slurry in kg N/m3")
    mix.add_argument("--area", metavar="HA", help="field area in hectares")
    mix.add_argument("--target", metavar="KG_N_PER_HA", help="nitrogen target in kg N/ha")
    mix.add_argument("--field", metavar="NAME", help="take area and target from a stored field")
    mix.add_argument("--batch", metavar="LABEL", help="take nitrogen content from a stored batch")
    mix.add_argument("--date", metavar="YYYY-MM-DD", help="season to report the remaining budget for")
    mix.set_defaults(func=cmd_mix)

    field_parser = sub.add_parser(
        "field",
        parents=[_global_parent()],
        help="register and list fields",
        description="Field register: name, area in hectares, crop and nitrogen target in kg N/ha.",
    )
    field_actions = field_parser.add_subparsers(dest="action", metavar="ACTION", title="actions")
    field_parser.set_defaults(func=cmd_field_list)
    field_add = field_actions.add_parser("add", parents=[_global_parent()], help="add a field")
    field_add.add_argument("--name", required=True, metavar="NAME", help="field name (must be unique)")
    field_add.add_argument("--area", required=True, metavar="HA", help="field area in hectares")
    field_add.add_argument("--crop", required=True, metavar="CROP", help="crop grown on the field")
    field_add.add_argument(
        "--target-n", required=True, metavar="KG_N_PER_HA", help="nitrogen target in kg N/ha"
    )
    field_add.set_defaults(func=cmd_field_add)
    field_actions.add_parser("list", parents=[_global_parent()], help="list fields").set_defaults(
        func=cmd_field_list
    )

    batch_parser = sub.add_parser(
        "batch",
        parents=[_global_parent()],
        help="record and list slurry analyses",
        description="Slurry batch register: nitrogen content in kg N/m3 and optional dry matter.",
    )
    batch_actions = batch_parser.add_subparsers(dest="action", metavar="ACTION", title="actions")
    batch_parser.set_defaults(func=cmd_batch_list)
    batch_add = batch_actions.add_parser("add", parents=[_global_parent()], help="record a slurry batch")
    batch_add.add_argument("--label", required=True, metavar="LABEL", help="batch label (must be unique)")
    batch_add.add_argument(
        "--source", required=True, metavar="SOURCE", help=f"one of: {', '.join(SOURCES)}"
    )
    batch_add.add_argument("--n", required=True, metavar="KG_N_PER_M3", help="nitrogen content in kg N/m3")
    batch_add.add_argument("--dm", metavar="PERCENT", help="dry matter percentage (0-100)")
    batch_add.add_argument("--sampled", metavar="YYYY-MM-DD", help="sampling date")
    batch_add.set_defaults(func=cmd_batch_add)
    batch_actions.add_parser("list", parents=[_global_parent()], help="list slurry batches").set_defaults(
        func=cmd_batch_list
    )

    plan_parser = sub.add_parser(
        "plan",
        parents=[_global_parent()],
        help="plan, list, remove and complete spreads",
        description="Season rotation plan: planned spreads ordered by date, with budget warnings.",
    )
    plan_actions = plan_parser.add_subparsers(dest="action", metavar="ACTION", title="actions")
    plan_parser.add_argument(
        "--all", action="store_true", help="list completed spreads as well as planned ones"
    )
    plan_parser.set_defaults(func=cmd_plan_list)
    plan_add = plan_actions.add_parser("add", parents=[_global_parent()], help="plan a spread")
    plan_add.add_argument("--field", required=True, metavar="NAME", help="field to spread on")
    plan_add.add_argument("--batch", required=True, metavar="LABEL", help="slurry batch to spread")
    plan_add.add_argument("--storage", required=True, metavar="NAME", help="storage unit the slurry comes from")
    plan_add.add_argument("--date", required=True, metavar="YYYY-MM-DD", help="planned spreading date")
    plan_add.add_argument(
        "--volume", metavar="M3", help="volume in m3 (default: calculated for the field's nitrogen target)"
    )
    plan_add.set_defaults(func=cmd_plan_add)
    plan_remove = plan_actions.add_parser("remove", parents=[_global_parent()], help="remove a planned spread")
    plan_remove.add_argument("--id", required=True, metavar="ID", help="plan entry id (from 'plan')")
    plan_remove.set_defaults(func=cmd_plan_remove)
    plan_done = plan_actions.add_parser(
        "done", parents=[_global_parent()], help="complete a spread and draw storage down"
    )
    plan_done.add_argument("--id", required=True, metavar="ID", help="plan entry id (from 'plan')")
    plan_done.set_defaults(func=cmd_plan_done)
    plan_list = plan_actions.add_parser("list", parents=[_global_parent()], help="list the rotation plan")
    plan_list.add_argument(
        "--all", action="store_true", help="list completed spreads as well as planned ones"
    )
    plan_list.set_defaults(func=cmd_plan_list)

    storage_parser = sub.add_parser(
        "storage",
        parents=[_global_parent()],
        help="track slurry levels against capacity",
        description="Storage tracker: level, capacity, free space and percent-full per unit.",
    )
    storage_actions = storage_parser.add_subparsers(dest="action", metavar="ACTION", title="actions")
    storage_parser.set_defaults(func=cmd_storage_list)
    storage_add = storage_actions.add_parser("add", parents=[_global_parent()], help="define a storage unit")
    storage_add.add_argument("--name", required=True, metavar="NAME", help="storage name (must be unique)")
    storage_add.add_argument("--capacity", required=True, metavar="M3", help="capacity in m3")
    storage_add.set_defaults(func=cmd_storage_add)
    storage_set = storage_actions.add_parser("set", parents=[_global_parent()], help="set a unit's level")
    storage_set.add_argument("--name", required=True, metavar="NAME", help="storage name")
    storage_set.add_argument("--level", required=True, metavar="M3", help="current level in m3")
    storage_set.set_defaults(func=cmd_storage_set)
    storage_actions.add_parser(
        "list", parents=[_global_parent()], help="show every storage unit"
    ).set_defaults(func=cmd_storage_list)

    sub.add_parser(
        "status",
        parents=[_global_parent()],
        help="one-screen summary of counts, nitrogen and storage",
        description="Record counts, per-field nitrogen against target, storage levels and days of spreading.",
    ).set_defaults(func=cmd_status)

    export = sub.add_parser(
        "export",
        parents=[_global_parent()],
        help="write plan.csv and storage.csv",
        description="Export the rotation plan and the storage levels as UTF-8 CSV.",
    )
    export.add_argument("--out", metavar="DIR", default=".", help="directory for the CSV files (default: .)")
    export.add_argument("--force", action="store_true", help="overwrite existing CSV files")
    export.set_defaults(func=cmd_export)

    sub.add_parser(
        "wizard",
        parents=[_global_parent()],
        help="prompt-driven flow through batch, plan and storage",
        description="Guided wizard: record a batch, plan a spread and view storage levels.",
    ).set_defaults(func=cmd_wizard)

    return parser


def _report(exc: CliError) -> int:
    err(f"{bold('error:')} {exc.message}")
    if exc.hint:
        err(f"hint: {exc.hint}")
    return exc.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    global _COLOR
    args_list = list(sys.argv[1:] if argv is None else argv)
    color_allowed = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    parser = build_parser()
    try:
        args = parser.parse_args(args_list)
    except CliError as exc:
        return _report(exc)
    except SystemExit as exc:  # --help and --version exit cleanly
        return int(exc.code or 0)
    _COLOR = color_allowed and not getattr(args, "no_color", False)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help()
        return 0
    path = state_path(args)
    try:
        return int(handler(args, path) or 0)
    except CliError as exc:
        return _report(exc)
    except KeyboardInterrupt:
        err("interrupted - nothing further was written")
        return 130


if __name__ == "__main__":
    sys.exit(main())
