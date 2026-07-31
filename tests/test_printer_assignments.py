"""Unit tests for printer assignment resolve + payload shaping (warocol.com#1949)."""
from uuid import uuid4

from app.services.printer_assignments_service import (
    PrinterAssignmentsPut,
    StationPrinterAssignment,
    _rows_to_payload,
    resolve_printer_name,
)


def test_resolve_uses_station_when_mapped():
    caja = "CAJA_PRINTER"
    station_id = str(uuid4())
    assert (
        resolve_printer_name(
            caja_printer_name=caja,
            station_map={station_id: "KITCHEN_A"},
            station_id=station_id,
        )
        == "KITCHEN_A"
    )


def test_resolve_falls_back_to_caja_when_station_unmapped():
    caja = "CAJA_PRINTER"
    station_id = str(uuid4())
    assert (
        resolve_printer_name(
            caja_printer_name=caja,
            station_map={},
            station_id=station_id,
        )
        == "CAJA_PRINTER"
    )


def test_resolve_returns_none_without_caja():
    station_id = str(uuid4())
    assert (
        resolve_printer_name(
            caja_printer_name=None,
            station_map={},
            station_id=station_id,
        )
        is None
    )


def test_resolve_caja_role_ignores_station_map_when_no_station():
    assert (
        resolve_printer_name(
            caja_printer_name="CAJA",
            station_map={str(uuid4()): "K"},
            station_id=None,
        )
        == "CAJA"
    )


def test_rows_to_payload_shapes_caja_and_stations():
    sid = uuid4()
    payload = _rows_to_payload(
        [
            {"role": "caja", "station_id": None, "printer_name": " STAR_TP586 "},
            {"role": "station", "station_id": sid, "printer_name": "BAR"},
        ]
    )
    assert payload["caja_printer_name"] == "STAR_TP586"
    assert payload["stations"] == [{"station_id": str(sid), "printer_name": "BAR"}]
    assert payload["station_map"][str(sid)] == "BAR"


def test_put_body_strips_empty_printer_names():
    body = PrinterAssignmentsPut(
        caja_printer_name="  ",
        stations=[
            StationPrinterAssignment(station_id=uuid4(), printer_name="  Kit  "),
            StationPrinterAssignment(station_id=uuid4(), printer_name=""),
        ],
    )
    assert body.caja_printer_name is None
    assert body.stations[0].printer_name == "Kit"
    assert body.stations[1].printer_name is None
