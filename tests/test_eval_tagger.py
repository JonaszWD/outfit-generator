from pathlib import Path

from app.services.tagging import FakeTagger
from scripts.eval_tagger import evaluate, format_report, is_correct, main, read_labels
from tests.conftest import image_bytes


def write_fixture(tmp_path: Path) -> Path:
    (tmp_path / "red.jpg").write_bytes(image_bytes(color="#c0392b", size=(90, 90)))
    (tmp_path / "bad.jpg").write_bytes(b"not an image")
    (tmp_path / "labels.csv").write_text(
        "filename,category,subtype,primary_color,pattern\n"
        "red.jpg,top,t-shirt,red,solid\n"
        "bad.jpg,top,shirt,blue,solid\n"
        "missing.jpg,top,,red,\n"
    )
    return tmp_path / "labels.csv"


def test_a_field_is_correct_only_on_an_exact_match() -> None:
    assert is_correct("subtype", "shirt", "shirt")
    assert not is_correct("subtype", "shirt", "blouse")


async def test_evaluate_reports_accuracy_mismatches_and_failures(tmp_path: Path) -> None:
    rows = read_labels(write_fixture(tmp_path))
    report = await evaluate(rows, lambda n: (tmp_path / n).read_bytes(), FakeTagger())
    assert report.evaluated == 1
    assert {name for name, _ in report.failures} == {"bad.jpg", "missing.jpg"}
    assert report.totals["primary_color"] == 1 and report.correct["primary_color"] == 1
    text = format_report(report)
    assert "primary_color" in text and "accuracy" in text and "Failures:" in text
    assert "bad.jpg" in text and "Mean confidence" in text


def test_main_runs_with_fake_backend(tmp_path: Path, capsys: object) -> None:
    write_fixture(tmp_path)
    assert main([str(tmp_path)]) == 0
