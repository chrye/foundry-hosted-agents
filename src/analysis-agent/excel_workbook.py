"""Bounded, in-memory .xlsx inspection and table calculations.

Tables use row 1 verbatim as unique, nonblank text headers. Pagination addresses
nonempty data rows, not worksheet row numbers. Completely empty literal rows
are skipped and counted; a formula without a cache is never an empty cell, while a
saved empty-string formula result is blank.
Blanks are None or the empty string, not zero, False, or whitespace.

Each sheet's grid runs from A1 to the last cell holding a value, inline string or
formula. Formatting-only cells, merged ranges and dimension hints never size the grid.
Chart sheets are skipped with a warning. Rows labelled like totals stay in calculations
but are reported, because they may double-count detail rows.
Only supplied formula caches are read; formulas and external links never run.
"""

from __future__ import annotations

import hashlib
import math
import posixpath
import re
import threading
import warnings
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, localcontext
from io import BytesIO
from pathlib import Path
from typing import Literal
from xml.etree.ElementTree import ParseError
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile
from zlib import error as ZlibError

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from openpyxl import load_workbook
from openpyxl.utils.cell import column_index_from_string, get_column_letter
from xlsx_attachments import MAX_WORKBOOK_BYTES

MAX_SHEETS = 20
MAX_CELLS = 100_000
MAX_EXPANDED_BYTES = 20 * 1024 * 1024
MAX_ZIP_MEMBERS = 512
MAX_PREVIEW_ROWS = 20
MAX_GROUPS = 1000
_EXCEL_ROWS = 1_048_576
_EXCEL_COLUMNS = 16_384
# warnings.catch_warnings swaps process-global state and parses run in worker threads.
_WARNINGS_LOCK = threading.Lock()

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_MAIN_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_SHEET_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
_COORDINATE = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})\Z")
_ROW_NUMBER = re.compile(r"[1-9][0-9]{0,6}\Z")
_FORMULA_WARNING = (
    "Formula values are cached results supplied by the workbook and may be stale. "
    "No formulas are recalculated."
)
_EXTERNAL_WARNING = (
    "External links are not followed or refreshed; only values already in the workbook are used."
)
_OPERATIONS = {"sum", "average", "min", "max", "count"}
_TOTALS_LABEL = re.compile(r"^(?:grand\s+)?(?:sub-?)?totals?\b|\b(?:sub-?)?totals?$", re.IGNORECASE)
type Scalar = str | int | float | bool | None
type Operation = Literal["sum", "average", "min", "max", "count"]


class ExcelError(ValueError):
    """The workbook or requested calculation is invalid or exceeds a limit."""


@dataclass(frozen=True)
class _SheetSpec:
    name: str
    path: str
    rows: int
    columns: int
    formulas: frozenset[tuple[int, int]]
    string_caches: frozenset[tuple[int, int]]
    state: str


@dataclass(frozen=True)
class _Cell:
    value: object = None
    kind: str = "n"
    formula: bool = False

    @property
    def empty(self) -> bool:
        # A formula with no saved result is unknown, not blank.
        return self.kind != "e" and _blank(self.value) and not (self.formula and self.value is None)


@dataclass(frozen=True)
class _Sheet:
    spec: _SheetSpec
    cells: tuple[tuple[_Cell, ...], ...]


@dataclass
class _Group:
    key: Scalar
    values: list[Decimal]
    count: int = 0
    blank_count: int = 0


def _blank(value: object) -> bool:
    return value is None or value == ""


def _xml_root(data: bytes):
    try:
        return SafeET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (DefusedXmlException, ParseError, LookupError) as exc:
        raise ExcelError(f"Invalid or unsafe workbook XML: {exc}") from exc


def _xml_events(data: bytes):
    try:
        with BytesIO(data) as stream:
            yield from SafeET.iterparse(
                stream, events=("start", "end"),
                forbid_dtd=True, forbid_entities=True, forbid_external=True,
            )
    except (DefusedXmlException, ParseError, LookupError) as exc:
        raise ExcelError(f"Invalid or unsafe workbook XML: {exc}") from exc


def _part_path(base: str, target: str) -> str:
    if not target or "\\" in target or ":" in target or "\x00" in target:
        raise ExcelError("Invalid internal workbook part path.")
    path = posixpath.normpath(
        target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(base), target)
    )
    if path in ("", ".", "..") or path.startswith("../"):
        raise ExcelError("Workbook part path escapes the ZIP package.")
    return path


def _coordinate(reference: str) -> tuple[int, int]:
    match = _COORDINATE.fullmatch(reference)
    if not match:
        raise ExcelError(f"Invalid or excessive worksheet coordinate: {reference!r}.")
    row = int(match[2])
    column = column_index_from_string(match[1])
    if row > _EXCEL_ROWS or column > _EXCEL_COLUMNS:
        raise ExcelError(f"Worksheet coordinate {reference!r} is outside Excel's grid.")
    return row, column


def _scan_sheet(data: bytes, name: str, remaining: int) -> tuple[int, int, frozenset, frozenset]:
    """Validate a worksheet and size it from cells holding a value, inline string or formula."""
    rows = columns = row_index = column_index = 0
    formulas: set[tuple[int, int]] = set()
    string_caches: set[tuple[int, int]] = set()
    stack: list[str] = []
    sheet_data_count = 0
    cell_fields: set[str] = set()
    cell_type = "n"
    has_content = False
    for event, element in _xml_events(data):
        tag = element.tag
        if event == "end":
            stack.pop()
            if tag == f"{{{_NS}}}v" and element.text:
                has_content = True
            elif tag == f"{{{_NS}}}c" and has_content:
                rows, columns = max(rows, row_index), max(columns, column_index)
                if rows * columns > remaining:
                    raise ExcelError(f"Workbook exceeds the {MAX_CELLS:,}-cell total grid limit.")
            element.clear()
            continue
        parent = stack[-1] if stack else None
        stack.append(tag)
        if len(stack) == 1 and tag != f"{{{_NS}}}worksheet":
            raise ExcelError(f"Sheet {name!r} is not a supported .xlsx worksheet.")
        if tag == f"{{{_NS}}}sheetData":
            if parent != f"{{{_NS}}}worksheet":
                raise ExcelError(f"Invalid sheetData placement in {name!r}.")
            sheet_data_count += 1
        elif tag == f"{{{_NS}}}row":
            if parent != f"{{{_NS}}}sheetData":
                raise ExcelError(f"Invalid row placement in {name!r}.")
            reference = element.get("r", str(row_index + 1))
            if not _ROW_NUMBER.fullmatch(reference) or int(reference) > _EXCEL_ROWS:
                raise ExcelError(f"Invalid or excessive row coordinate in {name!r}.")
            next_row = int(reference)
            if next_row <= row_index:
                raise ExcelError(f"Duplicate or out-of-order rows in {name!r}.")
            row_index, column_index = next_row, 0
        elif tag == f"{{{_NS}}}c":
            if parent != f"{{{_NS}}}row":
                raise ExcelError(f"Invalid cell placement in {name!r}.")
            reference = element.get("r")
            cell_row, cell_column = (
                _coordinate(reference) if reference else (row_index, column_index + 1)
            )
            if cell_row != row_index or cell_column <= column_index:
                raise ExcelError(f"Duplicate, out-of-order, or mismatched cell coordinate in {name!r}.")
            if cell_column > _EXCEL_COLUMNS:
                raise ExcelError(f"Sheet {name!r} exceeds Excel's column limit.")
            cell_type = element.get("t", "n")
            if cell_type not in {"n", "s", "str", "inlineStr", "b", "d", "e"}:
                raise ExcelError(f"Unsupported cell type in {name!r}.")
            column_index = cell_column
            cell_fields.clear()
            has_content = False
        elif tag in {f"{{{_NS}}}f", f"{{{_NS}}}v", f"{{{_NS}}}is"}:
            if parent != f"{{{_NS}}}c" or tag in cell_fields:
                raise ExcelError(f"Invalid or duplicate cell value/formula in {name!r}.")
            cell_fields.add(tag)
            if tag != f"{{{_NS}}}v":
                has_content = True
                if tag == f"{{{_NS}}}f":
                    formulas.add((row_index, column_index))
            elif cell_type == "str":
                # Excel saves a formula that returned "" as t="str" with an empty <v>.
                string_caches.add((row_index, column_index))
    if sheet_data_count != 1:
        raise ExcelError(f"Sheet {name!r} must contain exactly one sheetData element.")
    return rows, columns, frozenset(formulas), frozenset(string_caches)


def _package_specs(data: bytes) -> tuple[list[_SheetSpec], list[str]]:
    parts: dict[str, bytes] = {}
    try:
        with BytesIO(data) as buffer, ZipFile(buffer) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_MEMBERS:
                raise ExcelError(f"Workbook ZIP exceeds the {MAX_ZIP_MEMBERS}-member limit.")
            if sum(member.file_size for member in members) > MAX_EXPANDED_BYTES:
                raise ExcelError(
                    f"Workbook ZIP-expanded size exceeds the {MAX_EXPANDED_BYTES:,}-byte limit."
                )
            total = 0
            seen: set[str] = set()
            for member in members:
                name = member.filename
                normalized = name.rstrip("/")
                if (not normalized or name.startswith("/") or "\\" in name or "\x00" in name
                        or ":" in name or any(part in ("", ".", "..") for part in normalized.split("/"))
                        or normalized.casefold() in seen):
                    raise ExcelError("Workbook ZIP has duplicate or unsafe member names.")
                seen.add(normalized.casefold())
                if member.flag_bits & 0x41:
                    raise ExcelError("Encrypted workbook ZIP members are not supported.")
                if member.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
                    raise ExcelError("Unsupported workbook ZIP compression.")
                if any(marker in name.lower() for marker in ("vba", "macrosheet")):
                    raise ExcelError("Macro workbook parts are not supported.")
                with archive.open(member) as source:
                    content = source.read(MAX_EXPANDED_BYTES - total + 1)
                total += len(content)
                if total > MAX_EXPANDED_BYTES or len(content) != member.file_size:
                    raise ExcelError("Workbook ZIP-expanded size exceeds its limit or declared size.")
                if not member.is_dir():
                    parts[name] = content
    except ExcelError:
        raise
    except (BadZipFile, ZlibError, EOFError, NotImplementedError, ValueError) as exc:
        # zipfile also reports corrupt or unsupported archives as ValueError (for example, a
        # negative member seek) or NotImplementedError (for example, an unknown ZIP version).
        raise ExcelError(f"Invalid, corrupt or unsupported .xlsx ZIP package: {exc}") from exc

    if "[Content_Types].xml" not in parts:
        raise ExcelError("Invalid .xlsx package: missing content types.")
    types = _xml_root(parts["[Content_Types].xml"])
    if types.tag != f"{{{_CT}}}Types":
        raise ExcelError("Invalid .xlsx content type manifest.")
    overrides: dict[str, str] = {}
    for entry in types:
        content_type = entry.get("ContentType", "")
        if any(marker in content_type.lower() for marker in ("macro", "vba")):
            raise ExcelError("Macro-enabled workbooks are not supported.")
        if entry.tag == f"{{{_CT}}}Override":
            path = _part_path("", entry.get("PartName", ""))
            if path in overrides:
                raise ExcelError("Duplicate workbook content type declarations.")
            overrides[path] = content_type
    main_parts = [path for path, content_type in overrides.items() if content_type == _MAIN_TYPE]
    if len(main_parts) != 1 or main_parts[0] not in parts:
        raise ExcelError("Unsupported or missing .xlsx workbook part; only ordinary .xlsx files are supported.")
    main_path = main_parts[0]
    workbook = _xml_root(parts[main_path])
    if workbook.tag != f"{{{_NS}}}workbook":
        raise ExcelError("Unsupported workbook XML format.")
    relation_path = posixpath.join(
        posixpath.dirname(main_path), "_rels", posixpath.basename(main_path) + ".rels"
    )
    if relation_path not in parts:
        raise ExcelError("Missing workbook worksheet relationships.")
    relations = _xml_root(parts[relation_path])
    relation_map = {}
    for relation in relations:
        identifier = relation.get("Id")
        if not identifier or identifier in relation_map:
            raise ExcelError("Invalid or duplicate workbook relationship IDs.")
        relation_map[identifier] = relation

    entries = workbook.findall(f"{{{_NS}}}sheets/{{{_NS}}}sheet")
    if not entries or len(entries) > MAX_SHEETS:
        raise ExcelError(f"Workbook must contain 1 to {MAX_SHEETS} sheets (sheet limit).")
    specs = []
    sheet_names: list[str] = []
    notices: list[str] = []
    names: set[str] = set()
    sheet_paths: set[str] = set()
    cells = 0
    for entry in entries:
        name = entry.get("name", "")
        if not name or name.casefold() in names:
            raise ExcelError("Worksheet names must be nonempty and unique.")
        names.add(name.casefold())
        sheet_names.append(name)
        relation = relation_map.get(entry.get(f"{{{_DOC_REL}}}id"))
        relation_type = relation.get("Type") if relation is not None else None
        if (relation is None or relation.get("TargetMode", "").lower() == "external"
                or relation_type not in (f"{_DOC_REL}/worksheet", f"{_DOC_REL}/chartsheet")):
            raise ExcelError(f"Sheet {name!r} has an unsupported or external worksheet relationship.")
        if relation_type == f"{_DOC_REL}/chartsheet":
            notices.append(f"Chart sheet {name!r} was skipped; only worksheets contain table data.")
            continue
        path = _part_path(main_path, relation.get("Target", ""))
        if path not in parts or path in sheet_paths or overrides.get(path) != _SHEET_TYPE:
            raise ExcelError(f"Sheet {name!r} has a missing, duplicate, or unsupported worksheet part.")
        sheet_paths.add(path)
        rows, columns, formulas, string_caches = _scan_sheet(parts[path], name, MAX_CELLS - cells)
        cells += rows * columns
        specs.append(_SheetSpec(
            name, path, rows, columns, formulas, string_caches, entry.get("state", "visible"),
        ))

    external = False
    for path, content in parts.items():
        if path in sheet_paths or not path.lower().endswith((".xml", ".rels")):
            continue
        for event, element in _xml_events(content):
            if event == "start" and element.tag == f"{{{_REL}}}Relationship":
                if any(marker in element.get("Type", "").lower() for marker in ("vba", "macro")):
                    raise ExcelError("Macro relationships are not supported.")
                external |= element.get("TargetMode", "").lower() == "external"
            if event == "end":
                element.clear()
    return specs, sheet_names, notices + ([_EXTERNAL_WARNING] if external else [])


def _totals_label(row: tuple[_Cell, ...]) -> str | None:
    for cell in row:
        if isinstance(cell.value, str):
            label = cell.value.strip()
            if len(label.split()) <= 4 and _TOTALS_LABEL.search(label):
                return label
    return None


def _scalar(cell: _Cell, location: str) -> Scalar:
    if cell.formula and cell.value is None:
        raise ExcelError(f"Missing cached formula result at {location}; recalculate and save in Excel.")
    if cell.kind == "e":
        raise ExcelError(f"Excel error at {location}: {cell.value}.")
    value = cell.value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise ExcelError(f"Non-finite numeric value at {location}.")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ExcelError(f"Unsupported cell value type at {location}: {type(value).__name__}.")


def _precision(values: list[Decimal]) -> int:
    nonzero = [value for value in values if value]
    integer_places = max((value.adjusted() + 1 for value in nonzero), default=1)
    exponents = [value.as_tuple().exponent for value in nonzero]
    if any(not isinstance(exponent, int) for exponent in exponents):
        raise ExcelError("Calculation requires finite decimal values.")
    fraction_places = max((-exponent for exponent in exponents if isinstance(exponent, int)), default=0)
    return max(28, max(0, integer_places) + max(0, fraction_places) + len(str(len(values))) + 8)


def _sum(values: list[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = _precision(values)
        return sum(values, Decimal(0))


def _json_number(value: Decimal) -> int | float:
    if not value.is_finite():
        raise ExcelError("Calculation produced a non-finite number.")
    if value == value.to_integral_value():
        return int(value)
    number = float(value)
    if not math.isfinite(number) or (number == 0 and value != 0):
        raise ExcelError("Calculation result cannot be represented as a finite JSON number.")
    return number


def _numeric_result(group: _Group, operation: Operation) -> Decimal:
    if not group.values:
        raise ExcelError(f"All values are blank for group {group.key!r}; no numeric result exists.")
    if operation == "min":
        return min(group.values)
    if operation == "max":
        return max(group.values)
    total = _sum(group.values)
    if operation == "average":
        with localcontext() as context:
            context.prec = _precision([total, Decimal(group.count)]) + 32
            return total / Decimal(group.count)
    return total


class ExcelWorkbook:
    """Immutable parsed cell data with deterministic, explicitly bounded operations."""

    def __init__(self, filename: str, data: bytes):
        if not isinstance(filename, str) or not filename.strip() or not filename.lower().endswith(".xlsx"):
            raise ExcelError("Excel input requires a nonempty .xlsx filename.")
        if not isinstance(data, bytes) or not data:
            raise ExcelError("Excel input must be nonempty bytes.")
        if len(data) > MAX_WORKBOOK_BYTES:
            raise ExcelError(f"Excel workbook exceeds the {MAX_WORKBOOK_BYTES:,}-byte file size limit.")
        if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise ExcelError("Encrypted or legacy binary Excel workbooks are not supported; supply an unencrypted .xlsx.")
        specs, sheet_names, package_warnings = _package_specs(data)
        self.filename = filename
        self._bytes = len(data)
        self._sha256 = hashlib.sha256(data).hexdigest()
        self._sheets: dict[str, _Sheet] = {}
        self._warnings = package_warnings
        if any(spec.formulas for spec in specs):
            self._warnings.append(_FORMULA_WARNING)

        try:
            with _WARNINGS_LOCK, ExitStack() as resources, warnings.catch_warnings(record=True) as notices:
                warnings.simplefilter("always")
                formula_stream = resources.enter_context(BytesIO(data))
                formula_book = load_workbook(
                    formula_stream, read_only=True, data_only=False, keep_links=False, keep_vba=False,
                )
                resources.callback(formula_book.close)
                value_stream = resources.enter_context(BytesIO(data))
                value_book = load_workbook(
                    value_stream, read_only=True, data_only=True, keep_links=False, keep_vba=False,
                )
                resources.callback(value_book.close)
                if formula_book.sheetnames != sheet_names or value_book.sheetnames != sheet_names:
                    raise ExcelError("Worksheet metadata differs from the validated workbook package.")
                for spec in specs:
                    parsed = []
                    if spec.rows and spec.columns:
                        worksheet = value_book[spec.name]
                        worksheet.reset_dimensions()
                        with closing(worksheet.iter_rows(
                            min_row=1, max_row=spec.rows, min_col=1, max_col=spec.columns,
                        )) as rows:
                            for index, row in enumerate(rows, 1):
                                # The XML scan records formulas without expanding shared formulas.
                                parsed.append(tuple(
                                    _Cell(
                                        "" if cell.value is None and (index, column) in spec.string_caches
                                        else cell.value,
                                        cell.data_type,
                                        (index, column) in spec.formulas,
                                    )
                                    for column, cell in enumerate(row, 1)
                                ))
                        while len(parsed) < spec.rows:
                            parsed.append((_Cell(),) * spec.columns)
                    self._sheets[spec.name] = _Sheet(spec, tuple(parsed))
                self._warnings.extend(dict.fromkeys(
                    str(notice.message) for notice in notices if "openpyxl" in Path(notice.filename).parts
                ))
        except ExcelError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError,
                ParseError, DefusedXmlException, BadZipFile, ZlibError, EOFError) as exc:
            # openpyxl reports malformed OOXML through these parser/conversion exceptions.
            raise ExcelError(f"Invalid or unsupported .xlsx workbook: {exc}") from exc

    def inspect(self) -> dict:
        """Describe all sheets, including empty sheets and invalid table headers."""
        sheets = []
        for sheet in self._sheets.values():
            spec = sheet.spec
            headers = []
            for column, cell in enumerate(sheet.cells[0] if sheet.cells else (), 1):
                try:
                    headers.append(_scalar(cell, f"{spec.name}!{get_column_letter(column)}1"))
                except ExcelError as exc:
                    headers.append({"error": str(exc)})
            sheets.append({
                "name": spec.name, "rows": spec.rows, "columns": spec.columns,
                "data_rows": sum(not all(cell.empty for cell in row) for row in sheet.cells[1:]),
                "headers": headers, "state": spec.state, "formula_cells": len(spec.formulas),
                "missing_formula_results": sum(
                    sheet.cells[row - 1][column - 1].value is None for row, column in spec.formulas
                ),
            })
        return {
            "filename": self.filename, "bytes": self._bytes, "sha256": self._sha256,
            "sheets": sheets, "total_data_rows": sum(sheet["data_rows"] for sheet in sheets),
            "warnings": list(self._warnings),
        }

    def _table(self, name: str):
        if not isinstance(name, str) or name not in self._sheets:
            raise ExcelError(f"Unknown worksheet: {name!r}.")
        sheet = self._sheets[name]
        if not sheet.cells:
            raise ExcelError(f"Sheet {name!r} is empty; table operations need headers and data.")
        headers: list[str] = []
        for column, cell in enumerate(sheet.cells[0], 1):
            header = _scalar(cell, f"{name}!{get_column_letter(column)}1")
            if not isinstance(header, str) or not header.strip():
                raise ExcelError(f"Sheet {name!r} needs nonblank text headers in every column of row 1.")
            if header in headers:
                raise ExcelError(f"Sheet {name!r} has duplicate header {header!r}.")
            headers.append(header)
        rows = [(index, row) for index, row in enumerate(sheet.cells[1:], 2)
                if any(not cell.empty for cell in row)]
        if not rows:
            raise ExcelError(f"Sheet {name!r} has no nonempty data rows.")
        skipped = len(sheet.cells) - 1 - len(rows)
        return headers, rows, skipped

    def _table_warnings(self, sheet: str, skipped: int, rows: list) -> list[str]:
        result = list(self._warnings)
        if skipped:
            result.append(
                f"Sheet {sheet!r}: skipped {skipped} completely empty data rows; "
                "row counts and offsets refer to nonempty data rows."
            )
        labelled = [(index, label) for index, row in rows if (label := _totals_label(row))]
        if labelled:
            listed = ", ".join(f"row {index} ({label!r})" for index, label in labelled[:5])
            more = f" and {len(labelled) - 5} more" if len(labelled) > 5 else ""
            result.append(
                f"Sheet {sheet!r} has rows labelled like totals: {listed}{more}. They are "
                "included in calculations and may double-count detail rows."
            )
        return result

    @staticmethod
    def _column(headers: list[str], name: str) -> int:
        if not isinstance(name, str) or name not in headers:
            raise ExcelError(f"Unknown table column: {name!r}.")
        return headers.index(name)

    def read_rows(self, sheet: str, offset: int = 0, limit: int = 10) -> dict:
        """Preview at most 20 nonempty rows; only accessed cells need valid caches."""
        if type(offset) is not int or offset < 0:
            raise ExcelError("Row offset must be a nonnegative integer.")
        if type(limit) is not int or not 1 <= limit <= MAX_PREVIEW_ROWS:
            raise ExcelError(f"Preview limit must be an integer from 1 to {MAX_PREVIEW_ROWS}.")
        headers, data_rows, skipped = self._table(sheet)
        rows = [{
            header: _scalar(row[column], f"{sheet}!{get_column_letter(column + 1)}{index}")
            for column, header in enumerate(headers)
        } for index, row in data_rows[offset:offset + limit]]
        return {
            "sheet": sheet, "columns": headers, "offset": offset, "returned_rows": len(rows),
            "total_rows": len(data_rows), "rows": rows, "has_more": offset + len(rows) < len(data_rows),
            "skipped_empty_rows": skipped, "warnings": self._table_warnings(sheet, skipped, data_rows),
        }

    @staticmethod
    def _key(cell: _Cell, location: str, identities: dict) -> tuple[tuple, Scalar]:
        value = _scalar(cell, location)
        if _blank(value) or (isinstance(value, str) and not value.strip()):
            raise ExcelError(f"Group key at {location} is blank.")
        if isinstance(value, bool):
            token = ("boolean", value)
            source_type = "boolean"
        elif isinstance(value, (int, float)):
            token = ("number", Decimal(str(value)))
            source_type = "number"
        else:
            token = ("text", value)
            source_type = type(cell.value).__name__
        previous_type = identities.setdefault(token, source_type)
        if previous_type != source_type:
            raise ExcelError(f"Ambiguous group key {value!r} at {location}: {previous_type} versus {source_type}.")
        return token, value

    def _groups(self, sheet: str, value_column: str, group_by: str | None,
                operation: Operation, identities: dict):
        headers, rows, skipped = self._table(sheet)
        value_index = self._column(headers, value_column)
        key_index = self._column(headers, group_by) if group_by is not None else None
        groups: dict[tuple, _Group] = {}
        for row_number, row in rows:
            token, key = (("ungrouped",), None) if key_index is None else self._key(
                row[key_index], f"{sheet}!{get_column_letter(key_index + 1)}{row_number}", identities,
            )
            if token not in groups:
                if len(groups) >= MAX_GROUPS:
                    raise ExcelError(f"Result exceeds the {MAX_GROUPS}-group limit; no groups were truncated.")
                groups[token] = _Group(key, [])
            group = groups[token]
            location = f"{sheet}!{get_column_letter(value_index + 1)}{row_number}"
            value = _scalar(row[value_index], location)
            if _blank(value):
                group.blank_count += 1
                continue
            if operation != "count":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ExcelError(f"Numeric operation requires numbers, not {type(value).__name__}, at {location}.")
                group.values.append(Decimal(str(value)))
            group.count += 1
        return groups, len(rows), self._table_warnings(sheet, skipped, rows)

    def aggregate(self, sheet: str, value_column: str, operation: Operation,
                  group_by: str | None = None) -> dict:
        if not isinstance(operation, str) or operation not in _OPERATIONS:
            raise ExcelError("Operation must be sum, average, min, max, or count.")
        groups, rows, notices = self._groups(sheet, value_column, group_by, operation, {})
        results, without_numbers = [], []
        for group in groups.values():
            if operation == "count":
                value = group.count
            elif group.values or group_by is None:
                value = _json_number(_numeric_result(group, operation))
            else:
                # One empty group must not discard every other group's result, or become 0.
                value = None
                without_numbers.append(group.key)
            results.append({"key": group.key, "value": value, "count": group.count,
                            "blank_count": group.blank_count})
        if without_numbers:
            more = f" and {len(without_numbers) - 5} more" if len(without_numbers) > 5 else ""
            notices.append(
                f"Groups with no numeric {value_column!r} values have a null {operation}: "
                f"{without_numbers[:5]!r}{more}."
            )
        return {
            "sheet": sheet, "value_column": value_column, "operation": operation,
            "group_by": group_by, "rows": rows, "groups": results, "warnings": notices,
        }

    @staticmethod
    def _comparison(actual: Decimal, target: Decimal, label: str, notices: list[str]) -> dict:
        ratio = None
        if target == 0:
            notices.append(f"{label}: target is zero; attainment_pct is null (division by zero).")
        else:
            with localcontext() as context:
                context.prec = _precision([actual, target]) + 32
                ratio = _json_number(
                    (Decimal(100) * actual / target).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                )
        return {
            "actual": _json_number(actual), "target": _json_number(target),
            "difference": _json_number(_sum([actual, target.copy_negate()])), "attainment_pct": ratio,
        }

    def compare(self, actual_sheet: str, target_sheet: str, key_column: str,
                actual_column: str, target_column: str) -> dict:
        identities: dict = {}
        actuals, _, actual_warnings = self._groups(
            actual_sheet, actual_column, key_column, "sum", identities,
        )
        targets, _, target_warnings = self._groups(
            target_sheet, target_column, key_column, "sum", identities,
        )
        if actuals.keys() != targets.keys():
            actual_only = [group.key for token, group in actuals.items() if token not in targets]
            target_only = [group.key for token, group in targets.items() if token not in actuals]
            raise ExcelError(
                f"Unmatched key sets: {len(actual_only)} actual-only keys {actual_only[:5]!r}; "
                f"{len(target_only)} target-only keys {target_only[:5]!r}. "
                "Both sheets must have the same keys."
            )
        notices = list(dict.fromkeys(actual_warnings + target_warnings))
        rows = []
        actual_totals, target_totals = [], []
        for token, actual_group in actuals.items():
            actual = _numeric_result(actual_group, "sum")
            target = _numeric_result(targets[token], "sum")
            actual_totals.append(actual)
            target_totals.append(target)
            rows.append({
                "key": actual_group.key,
                **self._comparison(actual, target, f"Key {actual_group.key!r}", notices),
            })
        totals = self._comparison(_sum(actual_totals), _sum(target_totals), "Totals", notices)
        return {
            "actual_sheet": actual_sheet, "target_sheet": target_sheet, "key_column": key_column,
            "actual_column": actual_column, "target_column": target_column,
            "rows": rows, "totals": totals, "warnings": notices,
        }
