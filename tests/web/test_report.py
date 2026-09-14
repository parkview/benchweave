from benchweave.web.report import build_report

DATA = {
    "name": "adc_1234_test_20260914_120000.csv",
    "meta": {"note": "power on", "sample_rate_hz": "100.0"},
    "sample_count": 4,
    "duration_s": 3.0,
    "series": [
        {
            "name": "Voltage",
            "unit": "V",
            "points": [[0.0, 0.0], [1.0, 3.3], [2.0, 3.3], [3.0, 3.3]],
        },
        {
            "name": "Current",
            "unit": "A",
            "points": [[0.0, 0.0], [1.0, 0.5], [2.0, 0.5], [3.0, 0.5]],
        },
    ],
}


def test_build_report_includes_series_and_notes() -> None:
    markers = [
        {"label": "A", "t": 1.0, "note": "ramp up"},
        {"label": "B", "t": 2.0, "note": "steady state"},
    ]
    html = build_report(DATA, markers, None, None)
    assert "Voltage" in html
    assert "Current" in html
    assert "ramp up" in html
    assert "steady state" in html
    # both letter badges present
    assert 'class="marker">A</text>' in html
    assert 'class="marker">B</text>' in html


def test_build_report_region_table_and_summary() -> None:
    markers: list[dict[str, object]] = []
    html = build_report(DATA, markers, 1.0, 3.0)
    assert "<table>" in html
    for col in ("Min", "Mean", "Max", "RMS", "Pk-Pk"):
        assert f"<th>{col}</th>" in html
    # Ah integral over the region (0.5 A for 2 s = 1 As = ~0.000278 Ah)
    assert "Ah" in html
    assert "Wh" in html


def test_build_report_escapes_note_html() -> None:
    markers = [{"label": "A", "t": 1.0, "note": "<script>alert(1)</script>"}]
    html = build_report(DATA, markers, None, None)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
