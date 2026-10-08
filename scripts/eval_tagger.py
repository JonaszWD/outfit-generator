"""Measure tagger accuracy on your own wardrobe photos.

    uv run python -m scripts.eval_tagger photos/ --labels photos/labels.csv

labels.csv needs a `filename` column (relative to the folder) and any of the columns
category, subtype, primary_color, pattern. Blank cells are skipped. The
tagger is the one chosen by TAGGER_BACKEND, and each image goes through the same
normalization the pipeline uses before it reaches the tagger.
"""

import argparse
import asyncio
import csv
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.services.imaging import InvalidImageError, process_image
from app.services.tagging import Tagger, TaggingError, build_tagger

FIELDS = ("category", "subtype", "primary_color", "pattern")


@dataclass(frozen=True)
class LabelRow:
    filename: str
    labels: dict[str, str]


@dataclass
class Report:
    evaluated: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    correct: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    mismatches: list[tuple[str, str, str, str, float]] = field(default_factory=list)
    conf_right: list[float] = field(default_factory=list)
    conf_wrong: list[float] = field(default_factory=list)


def read_labels(path: Path) -> list[LabelRow]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "filename" not in reader.fieldnames:
            raise SystemExit(f"{path}: needs a 'filename' column")
        return [
            LabelRow(
                row["filename"].strip(),
                {
                    k: (row.get(k) or "").strip().lower()
                    for k in FIELDS
                    if (row.get(k) or "").strip()
                },
            )
            for row in reader
            if (row.get("filename") or "").strip()
        ]


def is_correct(name: str, expected: str, got: str) -> bool:
    return expected == got


async def evaluate(
    rows: Sequence[LabelRow],
    load_image: Callable[[str], bytes],
    tagger: Tagger,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> Report:
    report = Report()
    for index, row in enumerate(rows, 1):
        if on_progress:
            on_progress(index, len(rows), row.filename)
        try:
            raw = load_image(row.filename)
            normalized = process_image(raw, max_bytes=len(raw) + 1).main
            result = await tagger.tag(normalized)
        except (OSError, InvalidImageError, TaggingError) as exc:
            report.failures.append((row.filename, str(exc) or type(exc).__name__))
            continue
        report.evaluated += 1
        predicted = {
            "category": result.category.value,
            "subtype": result.subtype,
            "primary_color": result.primary_color,
            "pattern": result.pattern or "",
        }
        for name, expected in row.labels.items():
            got = predicted[name]
            right = is_correct(name, expected, got)
            report.totals[name] += 1
            report.correct[name] += right
            confidence = result.confidence[name]
            (report.conf_right if right else report.conf_wrong).append(confidence)
            if not right:
                report.mismatches.append((row.filename, name, expected, got, confidence))
    return report


def _mean(values: list[float]) -> str:
    return f"{sum(values) / len(values):.2f}" if values else "n/a"


def format_report(report: Report) -> str:
    lines = [f"Evaluated {report.evaluated} image(s); {len(report.failures)} failed.", ""]
    lines.append(f"{'field':<15}{'correct':>8}{'total':>7}{'accuracy':>10}")
    for name in FIELDS:
        total = report.totals.get(name, 0)
        if not total:
            continue
        right = report.correct[name]
        lines.append(f"{name:<15}{right:>8}{total:>7}{right / total:>9.0%}")
    lines += [
        "",
        f"Mean confidence when right: {_mean(report.conf_right)}   "
        f"when wrong: {_mean(report.conf_wrong)}",
        "(If 'wrong' is not clearly lower, raise TAG_CONFIDENCE_THRESHOLD's trust with care.)",
    ]
    if report.mismatches:
        lines += ["", "Mismatches:", f"  {'file':<28}{'field':<15}{'expected':<14}{'got':<14}conf"]
        for filename, name, expected, got, conf in report.mismatches:
            lines.append(f"  {filename:<28}{name:<15}{expected:<14}{got:<14}{conf:.2f}")
    if report.failures:
        lines += ["", "Failures:"]
        lines += [f"  {filename}: {error}" for filename, error in report.failures]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("folder", type=Path, help="folder containing the images")
    parser.add_argument("--labels", type=Path, help="CSV file (default: <folder>/labels.csv)")
    parser.add_argument("--limit", type=int, help="only evaluate the first N rows")
    args = parser.parse_args(argv)

    labels_path = args.labels or args.folder / "labels.csv"
    rows = read_labels(labels_path)[: args.limit]
    settings = get_settings()
    try:
        tagger = build_tagger(settings)
    except TaggingError as exc:
        print(exc, file=sys.stderr)
        return 2
    model = settings.tagger_model if settings.tagger_backend == "claude" else "fake"
    print(f"Tagger: {settings.tagger_backend} ({model}); {len(rows)} image(s)\n")

    def progress(i: int, n: int, name: str) -> None:
        print(f"[{i}/{n}] {name}", file=sys.stderr)

    report = asyncio.run(
        evaluate(rows, lambda name: (args.folder / name).read_bytes(), tagger, progress)
    )
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
