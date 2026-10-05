import pytest
from goalkeeper.office_run import inspect_outputs, source_rows, office_pilot
from goalkeeper.evaluation import NotReady


def test_office_expected_data_is_synthetic_and_checks_missing_files(tmp_path):
    rows = source_rows()
    assert len(rows) == 18 and sum(r[2] * r[3] - r[4] for r in rows) == 297180
    assert inspect_outputs(tmp_path)["passed"] is False


async def test_real_office_execution_requires_own_authorization(tmp_path, monkeypatch):
    from goalkeeper import office_run

    monkeypatch.setattr(office_run, "DATA", tmp_path)
    with pytest.raises(NotReady, match="授权"):
        await office_pilot()
    assert not (tmp_path / "private/f1-run.json").exists()


def test_chart_identifier_conversion_preserves_chart_data_and_original(tmp_path):
    import zipfile

    from goalkeeper.office_finalize import finalize_office_delivery

    source = tmp_path / "source"
    source.mkdir()
    content = b'<c:chart xmlns:c="urn:chart"><c:axId val="-2068027336"/><c:crossAx val="-2068027336"/><c:v>-200</c:v></c:chart>'
    with zipfile.ZipFile(source / "deck.pptx", "w") as package:
        package.writestr("ppt/charts/chart1.xml", content)
        package.writestr("ppt/slides/slide1.xml", b"unchanged-slide")
    original = (source / "deck.pptx").read_bytes()
    receipt = finalize_office_delivery(source, tmp_path / "delivery", ["deck.pptx"])
    with zipfile.ZipFile(tmp_path / "delivery/deck.pptx") as package:
        corrected = package.read("ppt/charts/chart1.xml")
        assert corrected == content.replace(b"-2068027336", b"2226939960")
        assert package.read("ppt/slides/slide1.xml") == b"unchanged-slide"
    assert (source / "deck.pptx").read_bytes() == original
    assert receipt["files"][0]["chart_axis_identifiers_normalized"] == {"ppt/charts/chart1.xml": 2}
    assert receipt["semantic_content_modified"] is False
