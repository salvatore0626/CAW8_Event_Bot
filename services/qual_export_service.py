from __future__ import annotations

import html
import io
import re
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from database import get_connection


INVALID_XML_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


HEADERS = [
    "discord username",
    "discord display name",
    "qual number",
    "air to ground remarks",
    "air to air remarks",
    "formation remarks",
    "tanker remarks",
    "Landing remarks",
    "carrier remarks",
    "Verdict",
    "vibe_remarks",
    "submitted by",
]


# Excel style IDs
STYLE_NORMAL = 0
STYLE_HEADER = 1
STYLE_WRAP = 2
STYLE_NA = 3
STYLE_RED = 4
STYLE_ORANGE = 5
STYLE_YELLOW = 6
STYLE_GREEN = 7


RATING_STYLE_BY_VALUE = {
    None: STYLE_NA,
    0: STYLE_NA,
    1: STYLE_RED,
    2: STYLE_ORANGE,
    3: STYLE_YELLOW,
    4: STYLE_GREEN,
    5: STYLE_NA,  # Legacy "Computer" should not imply a performance color.
}


def now_ts() -> int:
    return int(time.time())


def zulu_timestamp_for_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%MZ")


def safe_xml_text(value: Any) -> str:
    if value is None:
        return ""

    text = str(value)
    text = INVALID_XML_RE.sub("", text)
    return html.escape(text, quote=False)


def clean_text(value: Any) -> str:
    if value is None:
        return ""

    return " ".join(str(value).strip().split())


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None

    try:
        return int(value)
    except Exception:
        return None


def bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None

    try:
        return bool(int(value))
    except Exception:
        text = str(value).strip().lower()

        if text in {"pass", "passed", "true", "yes", "y"}:
            return True

        if text in {"fail", "failed", "false", "no", "n"}:
            return False

        return None


def row_value(row: Any, *names: str, default: Any = None) -> Any:
    keys = set(row.keys())

    for name in names:
        if name in keys:
            return row[name]

    return default


def split_vibe_from_general_remarks(
    general_remarks: str | None,
) -> tuple[str | None, str | None]:
    # Support old/flexible qual_log rows that stored vibe remarks inside final remarks.
    if not general_remarks:
        return None, None

    lines = str(general_remarks).splitlines()
    final_lines: list[str] = []
    vibe_lines: list[str] = []
    in_vibes = False

    for line in lines:
        stripped = line.strip()

        if stripped.lower().startswith("vibes remarks:"):
            in_vibes = True
            vibe_lines.append(stripped.split(":", 1)[1].strip())
            continue

        if in_vibes:
            if stripped:
                vibe_lines.append(stripped)
            continue

        final_lines.append(line)

    final_text = "\n".join(final_lines).strip() or None
    vibe_text = "\n".join(vibe_lines).strip() or None

    return final_text, vibe_text


def table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (str(table_name),),
    ).fetchone()

    return row is not None


def column_letter(index: int) -> str:
    # 1-based column index to Excel letters. 1 -> A, 27 -> AA.
    result = ""

    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result

    return result


def rating_style(value: Any) -> int:
    rating = int_or_none(value)

    if rating is None:
        return STYLE_NA

    rating = max(0, min(5, rating))
    return RATING_STYLE_BY_VALUE.get(rating, STYLE_NA)


def verdict_text(value: Any) -> str:
    passed = bool_or_none(value)

    if passed is True:
        return "pass"

    if passed is False:
        return "fail"

    return ""


def verdict_style(value: Any) -> int:
    passed = bool_or_none(value)

    if passed is True:
        return STYLE_GREEN

    if passed is False:
        return STYLE_RED

    return STYLE_NA


def qual_attempt_rows() -> list[dict[str, Any]]:
    with get_connection() as conn:
        if not table_exists(conn, "qual_log"):
            return []

        rows = conn.execute(
            """
            SELECT
                q.*,
                u.discord_username AS stored_discord_username,
                u.display_name AS stored_display_name,
                iu.discord_username AS instructor_discord_username,
                iu.display_name AS instructor_display_name
            FROM qual_log q
            LEFT JOIN users u
                ON u.discord_id = q.applicant_discord_id
            LEFT JOIN users iu
                ON iu.discord_id = q.instructor_discord_id
            ORDER BY
                q.applicant_discord_id COLLATE NOCASE ASC,
                COALESCE(q.created_at, 0) ASC,
                q.id ASC
            """
        ).fetchall()

    per_user_count: dict[str, int] = {}
    result: list[dict[str, Any]] = []

    for row in rows:
        discord_id = clean_text(row_value(row, "applicant_discord_id"))
        user_key = discord_id or clean_text(row_value(row, "applicant_username")) or "unknown"
        per_user_count[user_key] = per_user_count.get(user_key, 0) + 1

        general_remarks = clean_text(row_value(row, "remarks", "final_remarks")) or None
        vibe_remarks = clean_text(row_value(row, "vibe_remarks", "vibes_remarks")) or None

        if vibe_remarks is None:
            general_remarks, extracted_vibe = split_vibe_from_general_remarks(general_remarks)
            vibe_remarks = extracted_vibe

        result.append(
            {
                "discord_username": (
                    clean_text(row_value(row, "stored_discord_username"))
                    or clean_text(row_value(row, "applicant_username"))
                    or discord_id
                ),
                "discord_display_name": (
                    clean_text(row_value(row, "stored_display_name"))
                    or clean_text(row_value(row, "applicant_username"))
                    or discord_id
                ),
                "qual_number": per_user_count[user_key],

                "ag_remarks": clean_text(row_value(row, "ag_remarks", "air_to_ground_range_remarks")),
                "ag_rating": row_value(row, "ag_rating", "air_to_ground_range_rating"),

                "aa_remarks": clean_text(row_value(row, "aa_remarks", "air_to_air_range_remarks")),
                "aa_rating": row_value(row, "aa_rating", "air_to_air_range_rating"),

                "formation_remarks": clean_text(row_value(row, "formation_remarks", "formation_flying_remarks")),
                "formation_rating": row_value(row, "formation_rating", "formation_flying_rating"),

                "tank_remarks": clean_text(row_value(row, "tank_remarks", "aerial_refueling_remarks")),
                "tank_rating": row_value(row, "tank_rating", "aerial_refueling_rating"),

                "landing_remarks": clean_text(row_value(row, "case1_remarks", "case_1_remarks")),
                "landing_rating": row_value(row, "case1_rating", "case_1_rating"),

                "carrier_remarks": clean_text(row_value(row, "carrier_remarks", "carrier_landing_remarks")),
                "carrier_rating": row_value(row, "carrier_rating", "carrier_landing_rating"),

                "verdict": verdict_text(row_value(row, "pass", "final_result")),
                "verdict_style": verdict_style(row_value(row, "pass", "final_result")),

                "vibe_remarks": vibe_remarks or "",
                "vibe_rating": row_value(row, "vibe_rating", "vibes_rating", "vibe"),

                "submitted_by": (
                    clean_text(row_value(row, "instructor_display_name"))
                    or clean_text(row_value(row, "instructor_discord_username"))
                    or clean_text(row_value(row, "instructor_username"))
                    or clean_text(row_value(row, "instructor_discord_id"))
                ),

                "created_at": int_or_none(row_value(row, "created_at")),
                "id": int_or_none(row_value(row, "id")),
            }
        )

    return result


def cell_xml(cell_ref: str, value: Any, style_id: int | None = None) -> str:
    style_attr = f' s="{style_id}"' if style_id is not None else ""

    if value is None:
        return f'<c r="{cell_ref}"{style_attr} t="inlineStr"><is><t></t></is></c>'

    if isinstance(value, bool):
        return f'<c r="{cell_ref}"{style_attr} t="b"><v>{1 if value else 0}</v></c>'

    if isinstance(value, int) and not isinstance(value, bool):
        return f'<c r="{cell_ref}"{style_attr}><v>{value}</v></c>'

    if isinstance(value, float):
        return f'<c r="{cell_ref}"{style_attr}><v>{value}</v></c>'

    text = safe_xml_text(value)
    preserve = ' xml:space="preserve"' if text.startswith(" ") or text.endswith(" ") else ""

    return (
        f'<c r="{cell_ref}"{style_attr} t="inlineStr">'
        f"<is><t{preserve}>{text}</t></is>"
        f"</c>"
    )


def row_cells_xml(row_index: int, values: list[tuple[Any, int]]) -> str:
    cells = []

    for col_index, (value, style_id) in enumerate(values, start=1):
        cell_ref = f"{column_letter(col_index)}{row_index}"
        cells.append(cell_xml(cell_ref, value, style_id=style_id))

    return '<row r="{0}" spans="1:{1}">{2}</row>'.format(
        row_index,
        len(values),
        "".join(cells),
    )


def worksheet_xml(rows: list[dict[str, Any]]) -> str:
    max_cols = len(HEADERS)
    max_rows = max(1, len(rows) + 1)
    last_col = column_letter(max_cols)
    dimension = f"A1:{last_col}{max_rows}"

    widths = [
        22,  # A discord username
        24,  # B discord display name
        12,  # C qual number
        42,  # D A/G remarks
        42,  # E A/A remarks
        42,  # F formation remarks
        42,  # G tanker remarks
        42,  # H landing remarks
        42,  # I carrier remarks
        12,  # J verdict
        42,  # K vibe remarks
        26,  # L submitted by
    ]

    col_xml = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )

    row_xml_parts = []
    header_values = [(header, STYLE_HEADER) for header in HEADERS]
    row_xml_parts.append(row_cells_xml(1, header_values))

    for row_index, row in enumerate(rows, start=2):
        values: list[tuple[Any, int]] = [
            (row["discord_username"], STYLE_WRAP),
            (row["discord_display_name"], STYLE_WRAP),
            (row["qual_number"], STYLE_WRAP),

            (row["ag_remarks"], rating_style(row["ag_rating"])),
            (row["aa_remarks"], rating_style(row["aa_rating"])),
            (row["formation_remarks"], rating_style(row["formation_rating"])),
            (row["tank_remarks"], rating_style(row["tank_rating"])),
            (row["landing_remarks"], rating_style(row["landing_rating"])),
            (row["carrier_remarks"], rating_style(row["carrier_rating"])),

            (row["verdict"], int(row["verdict_style"])),

            (row["vibe_remarks"], rating_style(row["vibe_rating"])),
            (row["submitted_by"], STYLE_WRAP),
        ]
        row_xml_parts.append(row_cells_xml(row_index, values))

    auto_filter = f'<autoFilter ref="A1:{last_col}{max_rows}"/>'

    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
           xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
    <dimension ref="{dimension}"/>
    <sheetViews>
        <sheetView workbookViewId="0">
            <pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>
            <selection pane="bottomLeft"/>
        </sheetView>
    </sheetViews>
    <cols>{col_xml}</cols>
    <sheetData>{''.join(row_xml_parts)}</sheetData>
    {auto_filter}
</worksheet>
'''


def workbook_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
    <sheets>
        <sheet name="qual_attempts" sheetId="1" r:id="rId1"/>
    </sheets>
</workbook>
'''


def workbook_rels_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
    <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
    <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
'''


def root_rels_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
    <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
'''


def content_types_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
    <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
    <Default Extension="xml" ContentType="application/xml"/>
    <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
    <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
    <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
'''


def styles_xml() -> str:
    # style IDs:
    # 0 normal, 1 header, 2 wrapped, 3 white/NA, 4 red, 5 orange, 6 yellow, 7 green.
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
    <fonts count="3">
        <font><sz val="11"/><color rgb="FF111827"/><name val="Calibri"/><family val="2"/></font>
        <font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>
        <font><sz val="11"/><color rgb="FF111827"/><name val="Calibri"/><family val="2"/></font>
    </fonts>
    <fills count="8">
        <fill><patternFill patternType="none"/></fill>
        <fill><patternFill patternType="gray125"/></fill>
        <fill><patternFill patternType="solid"><fgColor rgb="FF1F2937"/><bgColor indexed="64"/></patternFill></fill>
        <fill><patternFill patternType="solid"><fgColor rgb="FFFFFFFF"/><bgColor indexed="64"/></patternFill></fill>
        <fill><patternFill patternType="solid"><fgColor rgb="FFF87171"/><bgColor indexed="64"/></patternFill></fill>
        <fill><patternFill patternType="solid"><fgColor rgb="FFFBBF24"/><bgColor indexed="64"/></patternFill></fill>
        <fill><patternFill patternType="solid"><fgColor rgb="FFFEF08A"/><bgColor indexed="64"/></patternFill></fill>
        <fill><patternFill patternType="solid"><fgColor rgb="FF86EFAC"/><bgColor indexed="64"/></patternFill></fill>
    </fills>
    <borders count="2">
        <border><left/><right/><top/><bottom/><diagonal/></border>
        <border>
            <left style="thin"><color rgb="FFD1D5DB"/></left>
            <right style="thin"><color rgb="FFD1D5DB"/></right>
            <top style="thin"><color rgb="FFD1D5DB"/></top>
            <bottom style="thin"><color rgb="FFD1D5DB"/></bottom>
            <diagonal/>
        </border>
    </borders>
    <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
    <cellXfs count="8">
        <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
        <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
        <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
        <xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
        <xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
        <xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
        <xf numFmtId="0" fontId="0" fillId="6" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
        <xf numFmtId="0" fontId="0" fillId="7" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
    </cellXfs>
    <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
    <dxfs count="0"/>
    <tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>
'''


def build_qual_export_xlsx_bytes() -> bytes:
    rows = qual_attempt_rows()
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types_xml())
        z.writestr("_rels/.rels", root_rels_xml())
        z.writestr("xl/workbook.xml", workbook_xml())
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml())
        z.writestr("xl/styles.xml", styles_xml())
        z.writestr("xl/worksheets/sheet1.xml", worksheet_xml(rows))

    return buffer.getvalue()


def create_qual_export_file() -> Path:
    export_dir = Path(tempfile.gettempdir()) / "airboss_qual_exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    filename = f"qual_attempts_{zulu_timestamp_for_filename()}.xlsx"
    path = export_dir / filename

    path.write_bytes(build_qual_export_xlsx_bytes())

    return path
