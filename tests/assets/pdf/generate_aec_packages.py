# ruff: noqa: E501, INP001
"""Generate deterministic AEC package PDFs for PDFValidator test harnesses.

The visible sheets resemble a small architectural coordination issue, while the
PDF object model carries synthetic IFC, IDS, and JSON payloads through the
small attachment-route allowlist in ``static_text_package_v1``. The negative
package adds inert declarations: the validator must reject the JavaScript
action and must never execute it or follow the out-of-scope URI hyperlink.

The payloads are intentionally small, repository-owned carrier fixtures.  They
exercise PDF package inspection and exact typed selection; they do not claim
IFC, IDS, PDF/A, drawing, or design conformance.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pypdf import PdfReader
from pypdf import PdfWriter
from pypdf.generic import ArrayObject
from pypdf.generic import DecodedStreamObject
from pypdf.generic import DictionaryObject
from pypdf.generic import FloatObject
from pypdf.generic import NameObject
from pypdf.generic import NumberObject
from pypdf.generic import TextStringObject
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A3
from reportlab.lib.pagesizes import landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

if TYPE_CHECKING:
    from collections.abc import Iterable

OUTPUT_DIR = Path(__file__).resolve().parent
CLEAN_PDF = OUTPUT_DIR / "aec-issue-package-clean.pdf"
NEGATIVE_PDF = OUTPUT_DIR / "aec-issue-package-negative.pdf"
SHEET_WIDTH, SHEET_HEIGHT = landscape(A3)

NAVY = HexColor("#123047")
BLUE = HexColor("#1D6A8A")
PALE_BLUE = HexColor("#EAF3F7")
GREEN = HexColor("#21835A")
PALE_GREEN = HexColor("#E8F4EE")
AMBER = HexColor("#D47B16")
PALE_AMBER = HexColor("#FFF3DF")
RED = HexColor("#A63D40")
PALE_RED = HexColor("#F8E8E8")
SLATE = HexColor("#486273")
LIGHT_LINE = HexColor("#C9D5DC")
GRID_LINE = HexColor("#DDE6EA")


IFC_PAYLOAD = b"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [CoordinationView]'),'2;1');
FILE_NAME('coordination-model.ifc','2026-08-12T00:00:00',
('Validibot Fixture Generator'),('McQuillen Interactive Pty Ltd'),
'Validibot AEC Fixture Generator','Validibot','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCPERSON($,'Fixture Generator',$,$,$,$,$,$);
#2=IFCORGANIZATION($,'McQuillen Interactive Pty Ltd',$,$,$);
#3=IFCPERSONANDORGANIZATION(#1,#2,$);
#4=IFCAPPLICATION(#2,'1.0','Validibot AEC Fixture Generator','VALIDIBOT');
#5=IFCOWNERHISTORY(#3,#4,$,.ADDED.,$,$,$,1786492800);
#6=IFCCARTESIANPOINT((0.,0.,0.));
#7=IFCAXIS2PLACEMENT3D(#6,$,$);
#8=IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.E-05,#7,$);
#9=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);
#10=IFCUNITASSIGNMENT((#9));
#11=IFCPROJECT('0JvctVUK5Azh9mwJB2pQHJ',#5,'Community Health Centre',
$,$,$,$,(#8),#10);
ENDSEC;
END-ISO-10303-21;
"""

IDS_PAYLOAD = b"""<?xml version="1.0" encoding="UTF-8"?>
<ids:ids xmlns:ids="http://standards.buildingsmart.org/IDS"
         xmlns:xs="http://www.w3.org/2001/XMLSchema"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://standards.buildingsmart.org/IDS https://standards.buildingsmart.org/IDS/1.0/ids.xsd">
  <ids:info>
    <ids:title>Validibot AEC coordination requirements</ids:title>
    <ids:version>1.0</ids:version>
    <ids:description>Synthetic test requirements for the attached IFC carrier.</ids:description>
    <ids:author>fixtures@validibot.com</ids:author>
    <ids:date>2026-08-12</ids:date>
  </ids:info>
  <ids:specifications>
    <ids:specification ifcVersion="IFC4" name="Walls need a fire rating" identifier="S1">
      <ids:applicability minOccurs="0" maxOccurs="unbounded">
        <ids:entity><ids:name><ids:simpleValue>IFCWALL</ids:simpleValue></ids:name></ids:entity>
      </ids:applicability>
      <ids:requirements>
        <ids:property dataType="IFCLABEL" cardinality="required">
          <ids:propertySet><ids:simpleValue>Pset_WallCommon</ids:simpleValue></ids:propertySet>
          <ids:baseName><ids:simpleValue>FireRating</ids:simpleValue></ids:baseName>
          <ids:value>
            <xs:restriction base="xs:string">
              <xs:enumeration value="REI30"/>
              <xs:enumeration value="REI60"/>
              <xs:enumeration value="REI90"/>
            </xs:restriction>
          </ids:value>
        </ids:property>
      </ids:requirements>
    </ids:specification>
  </ids:specifications>
</ids:ids>
"""

DUPLICATE_IDS_PAYLOAD = IDS_PAYLOAD.replace(
    b"Walls need a fire rating",
    b"Walls need an acoustic rating",
).replace(b"FireRating", b"AcousticRating")

UNSAFE_NOTES_PAYLOAD = b"""<?xml version="1.0" encoding="UTF-8"?>
<coordination-notes xmlns="urn:validibot:aec:fixture">
  <note>This inert member has an intentionally unsafe declared filename.</note>
</coordination-notes>
"""


@dataclass(frozen=True)
class PackageMember:
    """Describe one embedded payload and its untrusted PDF metadata."""

    logical_key: str
    filename: str
    data: bytes
    media_type: str
    relationship: str
    description: str


def _transmittal(*, negative: bool) -> bytes:
    """Return stable JSON bytes describing the synthetic drawing issue."""
    payload = {
        "document": {
            "issue": "P02",
            "number": "VB-AEC-A101",
            "purpose": "coordination",
            "status": "intentional-negative-test" if negative else "clean-test-package",
            "title": "Level 01 - Coordination Plan",
        },
        "package_id": (
            "VB-AEC-ISSUE-NEGATIVE-001" if negative else "VB-AEC-ISSUE-CLEAN-001"
        ),
        "project": {
            "name": "Community Health Centre",
            "number": "VB-AEC-001",
        },
        "revision_date": "2026-08-12",
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def _members(*, negative: bool) -> list[PackageMember]:
    """Return the embedded members for one package variant."""
    members = [
        PackageMember(
            logical_key="coordination-model",
            filename="coordination-model.ifc",
            data=IFC_PAYLOAD,
            media_type="text/plain" if negative else "model/step",
            relationship="Data",
            description="Synthetic IFC4 coordination model carrier",
        ),
        PackageMember(
            logical_key="requirements-primary",
            filename="requirements.ids",
            data=IDS_PAYLOAD,
            media_type="application/xml",
            relationship="Supplement",
            description="Synthetic buildingSMART IDS requirements",
        ),
        PackageMember(
            logical_key="transmittal",
            filename="transmittal.json",
            data=_transmittal(negative=negative),
            media_type="application/json",
            relationship="Data",
            description="Synthetic issue transmittal",
        ),
    ]
    if negative:
        members.extend(
            [
                PackageMember(
                    logical_key="requirements-duplicate",
                    filename="requirements.ids",
                    data=DUPLICATE_IDS_PAYLOAD,
                    media_type="application/xml",
                    relationship="Supplement",
                    description="Intentional ambiguous IDS selection candidate",
                ),
                PackageMember(
                    logical_key="unsafe-notes",
                    filename="../unsafe-notes.xml",
                    data=UNSAFE_NOTES_PAYLOAD,
                    media_type="application/xml",
                    relationship="Supplement",
                    description="Intentional filename-safety test member",
                ),
            ]
        )
    return members


def _line(
    drawing: canvas.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    width: float = 0.6,
    colour=SLATE,
) -> None:
    """Draw a consistently styled plan or table line."""
    drawing.setStrokeColor(colour)
    drawing.setLineWidth(width)
    drawing.line(x1, y1, x2, y2)


def _label(
    drawing: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    *,
    size: float = 8,
    colour=NAVY,
    font: str = "Helvetica",
) -> None:
    """Draw one stable ASCII label using a built-in PDF font."""
    drawing.setFillColor(colour)
    drawing.setFont(font, size)
    drawing.drawString(x, y, text)


def _centred_label(
    drawing: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    *,
    size: float = 8,
    colour=NAVY,
    font: str = "Helvetica",
) -> None:
    """Draw one centred plan label."""
    drawing.setFillColor(colour)
    drawing.setFont(font, size)
    drawing.drawCentredString(x, y, text)


def _status_banner(drawing: canvas.Canvas, *, negative: bool) -> None:
    """Draw a prominent package-state banner on every sheet."""
    colour = RED if negative else GREEN
    pale = PALE_RED if negative else PALE_GREEN
    text = (
        "INTENTIONAL NEGATIVE TEST PACKAGE"
        if negative
        else "VALIDATION TEST PACKAGE - CLEAN"
    )
    drawing.setFillColor(pale)
    drawing.roundRect(18 * mm, 276 * mm, 392 * mm, 10 * mm, 2 * mm, fill=1, stroke=0)
    drawing.setStrokeColor(colour)
    drawing.setLineWidth(1.2)
    drawing.roundRect(18 * mm, 276 * mm, 392 * mm, 10 * mm, 2 * mm, fill=0, stroke=1)
    _label(
        drawing,
        text,
        24 * mm,
        279.1 * mm,
        size=10,
        colour=colour,
        font="Helvetica-Bold",
    )
    _label(
        drawing,
        "SYNTHETIC - NOT FOR CONSTRUCTION",
        333 * mm,
        279.1 * mm,
        size=8,
        colour=colour,
        font="Helvetica-Bold",
    )


def _title_block(
    drawing: canvas.Canvas,
    *,
    sheet_number: str,
    sheet_title: str,
    negative: bool,
) -> None:
    """Draw an architectural title block with fixed issue metadata."""
    x, y, width, height = 18 * mm, 10 * mm, 392 * mm, 28 * mm
    drawing.setFillColor(colors.white)
    drawing.setStrokeColor(NAVY)
    drawing.setLineWidth(1)
    drawing.rect(x, y, width, height, fill=1, stroke=1)
    drawing.setFillColor(NAVY)
    drawing.rect(x, y, 72 * mm, height, fill=1, stroke=0)
    _label(
        drawing,
        "VALIDIBOT",
        x + 7 * mm,
        y + 15.5 * mm,
        size=17,
        colour=colors.white,
        font="Helvetica-Bold",
    )
    _label(
        drawing,
        "AEC PACKAGE VALIDATION FIXTURE",
        x + 7 * mm,
        y + 9.2 * mm,
        size=6.7,
        colour=colors.white,
        font="Helvetica-Bold",
    )
    _line(drawing, x + 72 * mm, y, x + 72 * mm, y + height, colour=NAVY)
    _line(drawing, x + 292 * mm, y, x + 292 * mm, y + height, colour=NAVY)
    _line(drawing, x + 342 * mm, y, x + 342 * mm, y + height, colour=NAVY)
    _label(
        drawing,
        "COMMUNITY HEALTH CENTRE",
        x + 78 * mm,
        y + 18.3 * mm,
        size=11,
        font="Helvetica-Bold",
    )
    _label(drawing, sheet_title.upper(), x + 78 * mm, y + 11.7 * mm, size=8.5)
    _label(drawing, "PROJECT VB-AEC-001", x + 78 * mm, y + 5.2 * mm, size=6.5)
    _label(drawing, "ISSUE", x + 298 * mm, y + 18.5 * mm, size=6, colour=SLATE)
    _label(drawing, "P02", x + 298 * mm, y + 8 * mm, size=14, font="Helvetica-Bold")
    _label(drawing, "DATE", x + 348 * mm, y + 18.5 * mm, size=6, colour=SLATE)
    _label(drawing, "2026-08-12", x + 348 * mm, y + 8.5 * mm, size=9)
    drawing.setFillColor(RED if negative else BLUE)
    drawing.rect(x + 375 * mm, y, 17 * mm, height, fill=1, stroke=0)
    _centred_label(
        drawing,
        sheet_number,
        x + 383.5 * mm,
        y + 11 * mm,
        size=13,
        colour=colors.white,
        font="Helvetica-Bold",
    )


def _room_tag(
    drawing: canvas.Canvas,
    name: str,
    number: str,
    x: float,
    y: float,
) -> None:
    """Draw a compact room name and number tag."""
    _centred_label(drawing, name, x, y + 2 * mm, size=7.2, font="Helvetica-Bold")
    _centred_label(drawing, number, x, y - 2 * mm, size=6.5, colour=SLATE)


def _draw_plan_sheet(drawing: canvas.Canvas, *, negative: bool) -> None:
    """Draw a polished but synthetic A3 architectural coordination plan."""
    _status_banner(drawing, negative=negative)
    _label(
        drawing,
        "LEVEL 01 - COORDINATION PLAN",
        18 * mm,
        261 * mm,
        size=15,
        font="Helvetica-Bold",
    )
    _label(
        drawing,
        "Visible sheet plus publisher-supplied IFC, IDS and transmittal data",
        18 * mm,
        254 * mm,
        size=8.5,
        colour=SLATE,
    )

    plan_x, plan_y, plan_w, plan_h = 28 * mm, 55 * mm, 272 * mm, 188 * mm
    for grid_x, grid_name in zip(
        [
            plan_x,
            plan_x + 68 * mm,
            plan_x + 136 * mm,
            plan_x + 204 * mm,
            plan_x + plan_w,
        ],
        ["1", "2", "3", "4", "5"],
        strict=True,
    ):
        _line(
            drawing,
            grid_x,
            plan_y - 6 * mm,
            grid_x,
            plan_y + plan_h + 6 * mm,
            colour=GRID_LINE,
        )
        drawing.setFillColor(colors.white)
        drawing.setStrokeColor(BLUE)
        drawing.circle(grid_x, plan_y + plan_h + 8 * mm, 3.5 * mm, fill=1, stroke=1)
        _centred_label(
            drawing,
            grid_name,
            grid_x,
            plan_y + plan_h + 6.9 * mm,
            size=7,
            font="Helvetica-Bold",
        )
    for grid_y, grid_name in zip(
        [plan_y, plan_y + 62.5 * mm, plan_y + 125 * mm, plan_y + plan_h],
        ["A", "B", "C", "D"],
        strict=True,
    ):
        _line(
            drawing,
            plan_x - 6 * mm,
            grid_y,
            plan_x + plan_w + 6 * mm,
            grid_y,
            colour=GRID_LINE,
        )
        drawing.setFillColor(colors.white)
        drawing.setStrokeColor(BLUE)
        drawing.circle(plan_x - 8 * mm, grid_y, 3.5 * mm, fill=1, stroke=1)
        _centred_label(
            drawing,
            grid_name,
            plan_x - 8 * mm,
            grid_y - 1.1 * mm,
            size=7,
            font="Helvetica-Bold",
        )

    drawing.setStrokeColor(NAVY)
    drawing.setLineWidth(3.2)
    drawing.rect(plan_x, plan_y, plan_w, plan_h, fill=0, stroke=1)
    drawing.setLineWidth(1.5)
    corridor_bottom = plan_y + 83 * mm
    corridor_top = corridor_bottom + 22 * mm
    drawing.line(plan_x, corridor_bottom, plan_x + plan_w, corridor_bottom)
    drawing.line(plan_x, corridor_top, plan_x + plan_w, corridor_top)
    for partition_x in [plan_x + 72 * mm, plan_x + 150 * mm, plan_x + 216 * mm]:
        drawing.line(partition_x, plan_y, partition_x, corridor_bottom)
        drawing.line(partition_x, corridor_top, partition_x, plan_y + plan_h)

    # Windows and door openings use a lighter line weight than walls.
    drawing.setStrokeColor(BLUE)
    drawing.setLineWidth(2)
    for window_x in [
        plan_x + 18 * mm,
        plan_x + 98 * mm,
        plan_x + 171 * mm,
        plan_x + 235 * mm,
    ]:
        drawing.line(window_x, plan_y + plan_h, window_x + 22 * mm, plan_y + plan_h)
        drawing.line(
            window_x,
            plan_y + plan_h - 1.5 * mm,
            window_x + 22 * mm,
            plan_y + plan_h - 1.5 * mm,
        )
    for window_x in [
        plan_x + 20 * mm,
        plan_x + 96 * mm,
        plan_x + 170 * mm,
        plan_x + 234 * mm,
    ]:
        drawing.line(window_x, plan_y, window_x + 20 * mm, plan_y)
        drawing.line(window_x, plan_y + 1.5 * mm, window_x + 20 * mm, plan_y + 1.5 * mm)

    # Simple plan symbols make the fixture immediately recognisable as an AEC sheet.
    drawing.setStrokeColor(SLATE)
    drawing.setLineWidth(0.7)
    for column_x in [
        plan_x + 6 * mm,
        plan_x + 68 * mm,
        plan_x + 136 * mm,
        plan_x + 204 * mm,
        plan_x + 266 * mm,
    ]:
        for column_y in [plan_y + 6 * mm, plan_y + 182 * mm]:
            drawing.setFillColor(PALE_BLUE)
            drawing.rect(
                column_x - 2 * mm, column_y - 2 * mm, 4 * mm, 4 * mm, fill=1, stroke=1
            )
    for door_x in [
        plan_x + 55 * mm,
        plan_x + 112 * mm,
        plan_x + 190 * mm,
        plan_x + 245 * mm,
    ]:
        drawing.arc(
            door_x - 8 * mm,
            corridor_bottom - 8 * mm,
            door_x + 8 * mm,
            corridor_bottom + 8 * mm,
            startAng=0,
            extent=90,
        )
        drawing.line(door_x, corridor_bottom, door_x + 8 * mm, corridor_bottom + 8 * mm)

    _room_tag(drawing, "CONSULT 01", "1.01", plan_x + 36 * mm, plan_y + 42 * mm)
    _room_tag(drawing, "CONSULT 02", "1.02", plan_x + 111 * mm, plan_y + 42 * mm)
    _room_tag(drawing, "TREATMENT", "1.03", plan_x + 183 * mm, plan_y + 42 * mm)
    _room_tag(drawing, "STAFF", "1.04", plan_x + 244 * mm, plan_y + 42 * mm)
    _room_tag(drawing, "WAITING", "1.05", plan_x + 37 * mm, plan_y + 147 * mm)
    _room_tag(drawing, "RECEPTION", "1.06", plan_x + 111 * mm, plan_y + 147 * mm)
    _room_tag(drawing, "MULTIPURPOSE", "1.07", plan_x + 183 * mm, plan_y + 147 * mm)
    _room_tag(drawing, "PLANT", "1.08", plan_x + 244 * mm, plan_y + 147 * mm)
    _centred_label(
        drawing,
        "MAIN CORRIDOR 1.09",
        plan_x + plan_w / 2,
        corridor_bottom + 8.2 * mm,
        size=7.5,
        colour=SLATE,
        font="Helvetica-Bold",
    )

    panel_x, panel_y, panel_w, panel_h = 310 * mm, 55 * mm, 100 * mm, 188 * mm
    drawing.setFillColor(PALE_BLUE)
    drawing.setStrokeColor(LIGHT_LINE)
    drawing.roundRect(panel_x, panel_y, panel_w, panel_h, 2 * mm, fill=1, stroke=1)
    _label(
        drawing,
        "PACKAGE CONTENTS",
        panel_x + 7 * mm,
        panel_y + panel_h - 14 * mm,
        size=10,
        font="Helvetica-Bold",
    )
    member_rows = [
        ("01", "coordination-model.ifc", "IFC4 / STEP P21"),
        ("02", "requirements.ids", "IDS 1.0 / XML"),
        ("03", "transmittal.json", "Issue metadata / JSON"),
    ]
    if negative:
        member_rows.extend(
            [
                ("04", "requirements.ids", "Duplicate selector candidate"),
                ("05", "../unsafe-notes.xml", "Unsafe declared name"),
            ]
        )
    row_y = panel_y + panel_h - 29 * mm
    for number, filename, description in member_rows:
        drawing.setFillColor(colors.white)
        drawing.setStrokeColor(LIGHT_LINE)
        drawing.roundRect(
            panel_x + 6 * mm,
            row_y - 7 * mm,
            88 * mm,
            16 * mm,
            1.5 * mm,
            fill=1,
            stroke=1,
        )
        drawing.setFillColor(BLUE if not negative else AMBER)
        drawing.circle(panel_x + 13 * mm, row_y + 1 * mm, 4 * mm, fill=1, stroke=0)
        _centred_label(
            drawing,
            number,
            panel_x + 13 * mm,
            row_y - 0.2 * mm,
            size=6.5,
            colour=colors.white,
            font="Helvetica-Bold",
        )
        _label(
            drawing,
            filename,
            panel_x + 21 * mm,
            row_y + 2.4 * mm,
            size=7.2,
            font="Helvetica-Bold",
        )
        _label(
            drawing,
            description,
            panel_x + 21 * mm,
            row_y - 2.8 * mm,
            size=6.2,
            colour=SLATE,
        )
        row_y -= 20 * mm

    note_y = panel_y + 28 * mm
    drawing.setFillColor(PALE_AMBER if negative else PALE_GREEN)
    drawing.roundRect(
        panel_x + 6 * mm, note_y, 88 * mm, 30 * mm, 2 * mm, fill=1, stroke=0
    )
    _label(
        drawing,
        "TEST INTENT",
        panel_x + 11 * mm,
        note_y + 20 * mm,
        size=7,
        colour=AMBER if negative else GREEN,
        font="Helvetica-Bold",
    )
    intent_lines = (
        [
            "Report active/external features",
            "Reject ambiguous IDS selection",
            "Flag type and filename hazards",
        ]
        if negative
        else [
            "Inventory three unique members",
            "Emit XML, JSON and STEP selections",
            "Pass static_text_package_v1",
        ]
    )
    for index, line_text in enumerate(intent_lines):
        _label(
            drawing,
            f"- {line_text}",
            panel_x + 11 * mm,
            note_y + (14 - index * 5) * mm,
            size=6.3,
            colour=SLATE,
        )

    # North arrow and scale bar reinforce the drawing-sheet character.
    drawing.setStrokeColor(NAVY)
    drawing.setFillColor(NAVY)
    drawing.setLineWidth(1.2)
    arrow_x, arrow_y = 292 * mm, 226 * mm
    drawing.line(arrow_x, arrow_y - 10 * mm, arrow_x, arrow_y + 5 * mm)
    drawing.line(arrow_x, arrow_y + 5 * mm, arrow_x - 3 * mm, arrow_y)
    drawing.line(arrow_x, arrow_y + 5 * mm, arrow_x + 3 * mm, arrow_y)
    _centred_label(
        drawing, "N", arrow_x, arrow_y + 7 * mm, size=8, font="Helvetica-Bold"
    )
    scale_x, scale_y = 28 * mm, 44 * mm
    for index in range(5):
        drawing.setFillColor(NAVY if index % 2 == 0 else colors.white)
        drawing.rect(
            scale_x + index * 10 * mm, scale_y, 10 * mm, 2.5 * mm, fill=1, stroke=1
        )
    _label(drawing, "0", scale_x, scale_y - 4 * mm, size=5.5)
    _label(drawing, "5 m", scale_x + 48 * mm, scale_y - 4 * mm, size=5.5)
    _label(
        drawing,
        "SCALE 1:100 AT A3",
        scale_x + 58 * mm,
        scale_y - 1.5 * mm,
        size=6.2,
        colour=SLATE,
    )

    _title_block(
        drawing,
        sheet_number="A101",
        sheet_title="Level 01 - Coordination Plan",
        negative=negative,
    )
    drawing.showPage()


def _table_row(
    drawing: canvas.Canvas,
    values: Iterable[str],
    widths: list[float],
    x: float,
    y: float,
    height: float,
    *,
    header: bool = False,
    negative: bool = False,
) -> None:
    """Draw one manifest table row with predictable wrapping-free content."""
    fill = (RED if negative else NAVY) if header else colors.white
    text_colour = colors.white if header else NAVY
    drawing.setFillColor(fill)
    drawing.setStrokeColor(LIGHT_LINE)
    offset = 0.0
    for value, width in zip(values, widths, strict=True):
        drawing.setFillColor(fill)
        drawing.rect(x + offset, y, width, height, fill=1, stroke=1)
        _label(
            drawing,
            value,
            x + offset + 3 * mm,
            y + height / 2 - 2.2,
            size=6.5 if not header else 6.8,
            colour=text_colour,
            font="Helvetica-Bold" if header else "Helvetica",
        )
        offset += width


def _draw_manifest_sheet(drawing: canvas.Canvas, *, negative: bool) -> None:
    """Draw the package manifest and expected validator outcomes."""
    _status_banner(drawing, negative=negative)
    _label(
        drawing,
        "DIGITAL DELIVERY MANIFEST",
        18 * mm,
        260 * mm,
        size=15,
        font="Helvetica-Bold",
    )
    _label(
        drawing,
        "Exact embedded-member identities and expected PDFValidator behavior",
        18 * mm,
        253 * mm,
        size=8.5,
        colour=SLATE,
    )

    x, table_top = 18 * mm, 232 * mm
    widths = [76 * mm, 53 * mm, 42 * mm, 47 * mm, 106 * mm, 68 * mm]
    headers = [
        "ORIGINAL NAME",
        "DECLARED TYPE",
        "DETECTED TYPE",
        "AF RELATION",
        "TEST PURPOSE",
        "TYPED OUTPUT",
    ]
    _table_row(
        drawing, headers, widths, x, table_top, 10 * mm, header=True, negative=negative
    )
    rows = [
        [
            "coordination-model.ifc",
            "text/plain" if negative else "model/step",
            "model/step",
            "Data",
            "IFC4 Part 21 carrier preflight",
            "selected_step_p21",
        ],
        [
            "requirements.ids",
            "application/xml",
            "application/xml",
            "Supplement",
            "IDS XML root and exact bytes",
            "selected_xml",
        ],
        [
            "transmittal.json",
            "application/json",
            "application/json",
            "Data",
            "Issue metadata and JSON syntax",
            "selected_json",
        ],
    ]
    if negative:
        rows.extend(
            [
                [
                    "requirements.ids",
                    "application/xml",
                    "application/xml",
                    "Supplement",
                    "Intentional ambiguous second match",
                    "selected_xml",
                ],
                [
                    "../unsafe-notes.xml",
                    "application/xml",
                    "application/xml",
                    "Supplement",
                    "Filename evidence must never become a path",
                    "none",
                ],
            ]
        )
    row_y = table_top - 10 * mm
    for values in rows:
        row_y -= 10 * mm
        _table_row(drawing, values, widths, x, row_y, 10 * mm)

    section_y = row_y - 18 * mm
    card_width = 188 * mm
    for card_index in range(2):
        card_x = 18 * mm + card_index * 202 * mm
        drawing.setFillColor(PALE_RED if negative else PALE_BLUE)
        drawing.setStrokeColor(LIGHT_LINE)
        drawing.roundRect(
            card_x, section_y - 76 * mm, card_width, 76 * mm, 2 * mm, fill=1, stroke=1
        )

    _label(
        drawing,
        "EXACT SELECTOR CONFIGURATION",
        26 * mm,
        section_y - 12 * mm,
        size=9,
        font="Helvetica-Bold",
    )
    selector_lines = [
        "XML  original_filename = requirements.ids",
        "     detected_media_type = application/xml",
        "     xml_root_qname = {http://standards.buildingsmart.org/IDS}ids",
        "JSON original_filename = transmittal.json",
        "     detected_media_type = application/json",
        "STEP original_filename = coordination-model.ifc",
        "     detected_media_type = model/step; FILE_SCHEMA = IFC4",
    ]
    for index, line_text in enumerate(selector_lines):
        _label(
            drawing,
            line_text,
            26 * mm,
            section_y - (23 + index * 7) * mm,
            size=6.7,
            font="Courier" if "=" in line_text else "Helvetica",
        )

    _label(
        drawing,
        "EXPECTED OUTCOME",
        228 * mm,
        section_y - 12 * mm,
        size=9,
        font="Helvetica-Bold",
    )
    outcome_lines = (
        [
            "static_text_package_v1: FAIL",
            "Duplicate requirements.ids names fail before any selection.",
            "Declared text/plain conflicts with the detected STEP carrier.",
            "The OpenAction JavaScript is rejected without being executed.",
            "The ../unsafe-notes.xml name remains inventory evidence only.",
            "No XMP, selected member, or extraction bundle is published.",
        ]
        if negative
        else [
            "static_text_package_v1: PASS",
            "Three unique embedded member byte sequences are inventoried.",
            "The deterministic extraction bundle contains all three members.",
            "XML, JSON and STEP selectors each emit exactly one payload.",
            "Selected bytes are unchanged and independently hashable.",
            "URI hyperlinks and signatures are outside this validator's scope.",
        ]
    )
    for index, line_text in enumerate(outcome_lines):
        _label(
            drawing,
            f"- {line_text}",
            228 * mm,
            section_y - (23 + index * 8) * mm,
            size=6.8,
            colour=RED if negative and index == 0 else NAVY,
            font="Helvetica-Bold" if index == 0 else "Helvetica",
        )

    disclaimer_y = 66 * mm
    drawing.setFillColor(PALE_AMBER)
    drawing.roundRect(
        18 * mm, disclaimer_y, 392 * mm, 20 * mm, 2 * mm, fill=1, stroke=0
    )
    _label(
        drawing,
        "SCOPE NOTE",
        26 * mm,
        disclaimer_y + 12 * mm,
        size=7,
        colour=AMBER,
        font="Helvetica-Bold",
    )
    _label(
        drawing,
        "This fixture validates the PDF package and typed carrier boundary only. It makes no claim of IFC, IDS, PDF/A, design or construction conformance.",
        26 * mm,
        disclaimer_y + 6 * mm,
        size=6.7,
        colour=SLATE,
    )
    _label(
        drawing,
        "Repository-owned synthetic data - CC0-1.0 - no customer, council or project information.",
        26 * mm,
        disclaimer_y + 2 * mm,
        size=6.2,
        colour=SLATE,
    )

    _title_block(
        drawing,
        sheet_number="G001",
        sheet_title="Digital Delivery Manifest",
        negative=negative,
    )
    drawing.showPage()


def _base_pdf(*, negative: bool) -> bytes:
    """Render the two visible A3 sheets into deterministic base PDF bytes."""
    output = io.BytesIO()
    drawing = canvas.Canvas(
        output,
        pagesize=landscape(A3),
        pageCompression=1,
        invariant=1,
    )
    variant = "Intentional negative" if negative else "Clean"
    drawing.setAuthor("McQuillen Interactive Pty. Ltd.")
    drawing.setCreator("Validibot AEC Fixture Generator")
    drawing.setTitle(f"Validibot AEC Issue Package - {variant}")
    drawing.setSubject("Synthetic PDF package fixture for the Validibot PDFValidator")
    _draw_plan_sheet(drawing, negative=negative)
    _draw_manifest_sheet(drawing, negative=negative)
    drawing.save()
    return output.getvalue()


def _xmp_packet(*, negative: bool) -> bytes:
    """Return deterministic document-level XMP and a test policy declaration."""
    package_id = "VB-AEC-ISSUE-NEGATIVE-001" if negative else "VB-AEC-ISSUE-CLEAN-001"
    status = "intentional-negative-test" if negative else "clean-test-package"
    return f"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
      xmlns:dc="http://purl.org/dc/elements/1.1/"
      xmlns:pdfd="http://pdfa.org/declarations/"
      xmlns:vb="urn:validibot:aec:fixture:">
      <dc:title><rdf:Alt><rdf:li xml:lang="x-default">Validibot AEC Issue Package</rdf:li></rdf:Alt></dc:title>
      <dc:identifier>{package_id}</dc:identifier>
      <vb:projectNumber>VB-AEC-001</vb:projectNumber>
      <vb:revision>P02</vb:revision>
      <vb:status>{status}</vb:status>
      <vb:securityPolicy>static_text_package_v1</vb:securityPolicy>
      <pdfd:declarations>
        <rdf:Bag><rdf:li>urn:validibot:aec-issue-package-fixture:v1</rdf:li></rdf:Bag>
      </pdfd:declarations>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
""".encode()


def _append_annotation(page, annotation) -> None:
    """Append an annotation without discarding reportlab-created URI links."""
    annotations = page.get("/Annots")
    if annotations is not None:
        annotations = annotations.get_object()
    if not isinstance(annotations, ArrayObject):
        annotations = ArrayObject()
        page[NameObject("/Annots")] = annotations
    annotations.append(annotation)


def _package_pdf(path: Path, *, negative: bool) -> None:
    """Attach typed members and inert hazards to the rendered A3 sheets."""
    members = _members(negative=negative)
    reader = PdfReader(io.BytesIO(_base_pdf(negative=negative)), strict=True)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer.pdf_header = "%PDF-2.0"

    specs_by_key = {}
    for member in members:
        embedded = writer.add_attachment(member.logical_key, member.data)
        embedded.alternative_name = TextStringObject(member.filename)
        embedded.description = TextStringObject(member.description)
        embedded.associated_file_relationship = NameObject(f"/{member.relationship}")
        embedded.subtype = NameObject(f"/{member.media_type}")
        embedded.size = NumberObject(len(member.data))
        file_spec = embedded.pdf_object
        embedded_file_reference = file_spec["/EF"].raw_get("/F")
        file_spec["/EF"][NameObject("/UF")] = embedded_file_reference
        specs_by_key[member.logical_key] = file_spec.indirect_reference

    writer.root_object[NameObject("/AF")] = ArrayObject(list(specs_by_key.values()))
    xmp = DecodedStreamObject()
    xmp.set_data(_xmp_packet(negative=negative))
    xmp.update(
        {
            NameObject("/Type"): NameObject("/Metadata"),
            NameObject("/Subtype"): NameObject("/XML"),
        }
    )
    writer.root_object[NameObject("/Metadata")] = writer._add_object(xmp)

    writer.pages[0][NameObject("/AF")] = ArrayObject(
        [specs_by_key["coordination-model"]]
    )
    writer.pages[1][NameObject("/AF")] = ArrayObject(
        [specs_by_key["requirements-primary"], specs_by_key["transmittal"]]
    )
    attachment = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/FileAttachment"),
            NameObject("/Rect"): ArrayObject(
                [
                    FloatObject(380 * mm),
                    FloatObject(236 * mm),
                    FloatObject(388 * mm),
                    FloatObject(244 * mm),
                ]
            ),
            NameObject("/FS"): specs_by_key["transmittal"],
        }
    )
    attachment_reference = writer._add_object(attachment)
    _append_annotation(writer.pages[1], attachment_reference)

    if negative:
        open_action = DictionaryObject(
            {
                NameObject("/S"): NameObject("/JavaScript"),
                NameObject("/JS"): TextStringObject(
                    "app.alert('Validibot must never execute this fixture')"
                ),
            }
        )
        writer.root_object[NameObject("/OpenAction")] = writer._add_object(open_action)
        uri_action = DictionaryObject(
            {
                NameObject("/S"): NameObject("/URI"),
                NameObject("/URI"): TextStringObject(
                    "https://example.invalid/aec-fixture-never-fetched"
                ),
            }
        )
        uri_action_reference = writer._add_object(uri_action)
        uri_annotation = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/Rect"): ArrayObject(
                    [
                        FloatObject(310 * mm),
                        FloatObject(45 * mm),
                        FloatObject(404 * mm),
                        FloatObject(51 * mm),
                    ]
                ),
                NameObject("/Border"): ArrayObject(
                    [FloatObject(0), FloatObject(0), FloatObject(0)]
                ),
                NameObject("/A"): uri_action_reference,
            }
        )
        uri_annotation_reference = writer._add_object(uri_annotation)
        _append_annotation(writer.pages[0], uri_annotation_reference)

    writer.add_metadata(
        {
            "/Title": (
                "Validibot AEC Issue Package - Intentional Negative"
                if negative
                else "Validibot AEC Issue Package - Clean"
            ),
            "/Author": "McQuillen Interactive Pty. Ltd.",
            "/Creator": "Validibot AEC Fixture Generator",
            "/Subject": "Synthetic PDF package fixture for the Validibot PDFValidator",
        }
    )
    with path.open("wb") as output:
        writer.write(output)


def main() -> None:
    """Regenerate both stable PDF assets in their committed directory."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _package_pdf(CLEAN_PDF, negative=False)
    _package_pdf(NEGATIVE_PDF, negative=True)


if __name__ == "__main__":
    main()
