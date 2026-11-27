#!/usr/bin/env python3
"""Parse a scientific PDF into evidence-linked structured JSON and Markdown."""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import traceback
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import pdfplumber
except ImportError:  # Reported later in a structured failure result.
    pdfplumber = None

try:
    from pypdf import PdfReader
except ImportError:  # pypdf is optional; pdfplumber still provides raw extraction
    PdfReader = None


CAPTION_RE = re.compile(
    r"^(?:(?:fig(?:ure)?\.?|table|tab\.?)\s*[A-Za-z]?\d+(?:[.\-]\d+)?"
    r"|图\s*\d+|表\s*\d+)\s*[:.\-]?\s*",
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"^(?:\d+(?:\.\d+){0,4}|[IVXLC]+)\.?\s+\S+")
REFERENCE_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*\s*)?(?:references|bibliography|literature cited|参考文献)\s*$",
    re.IGNORECASE,
)
ABSTRACT_RE = re.compile(r"^(?:abstract|摘要)\s*[:.]?\s*", re.IGNORECASE)
EQUATION_RE = re.compile(
    r"(?:[=≈≠≤≥<>]|\\(?:frac|sum|int|sqrt|alpha|beta|gamma|delta|theta|lambda)"
    r"|∑|∫|√|±|×|÷|∂|∇|→|←|↔|\b(?:sin|cos|tan|log|exp)\b)",
    re.IGNORECASE,
)
DOI_RE = re.compile(r"\b(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)", re.I)
ARXIV_RE = re.compile(r"\barXiv\s*:\s*(\d{4}\.\d{4,5}(?:v\d+)?)", re.I)
PAGE_NUMBER_RE = re.compile(r"^(?:page\s*)?(?:\d{1,4}|[ivxlcdm]{1,8})$", re.I)
BOLD_MARKERS = ("bold", "black", "heavy", "demi", "semibold", "medium")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def bbox_round(bbox: Iterable[float] | None) -> list[float] | None:
    if not bbox:
        return None
    try:
        return [round(float(value), 2) for value in bbox]
    except (TypeError, ValueError):
        return None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bbox_union(boxes: Iterable[Iterable[float] | None]) -> list[float] | None:
    valid = [list(map(float, box)) for box in boxes if box and len(box) == 4]
    if not valid:
        return None
    return bbox_round(
        [
            min(box[0] for box in valid),
            min(box[1] for box in valid),
            max(box[2] for box in valid),
            max(box[3] for box in valid),
        ]
    )


def intersection_area(a: Iterable[float], b: Iterable[float]) -> float:
    ax0, ay0, ax1, ay1 = map(float, a)
    bx0, by0, bx1, by1 = map(float, b)
    width = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    height = max(0.0, min(ay1, by1) - max(ay0, by0))
    return width * height


def intersection_over_line(line_bbox: Iterable[float], table_bbox: Iterable[float]) -> float:
    values = list(map(float, line_bbox))
    area = max(0.0, values[2] - values[0]) * max(0.0, values[3] - values[1])
    if area <= 0:
        return 0.0
    return intersection_area(values, table_bbox) / area


def normalized_margin_key(text: str) -> str:
    value = canonical_text(text).lower()
    value = re.sub(r"\d+", "#", value)
    value = re.sub(r"[^\w#]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def is_bold(fontname: str) -> bool:
    lowered = (fontname or "").lower()
    return any(marker in lowered for marker in BOLD_MARKERS)


def join_words(words: list[dict[str, Any]]) -> str:
    output = ""
    previous: dict[str, Any] | None = None
    no_space_before = set(".,;:!?%)]}，。；：！？、")
    no_space_after = set("([{（［【")
    for word in words:
        text = canonical_text(word.get("text", ""))
        if not text:
            continue
        if not output:
            output = text
        else:
            if text[:1] not in no_space_before and output[-1:] not in no_space_after:
                output += " "
            output += text
        previous = word
    return canonical_text(output)


def spacing_normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", canonical_text(text))
    return "".join(char for char in value if not char.isspace() and char != "\u00ad")


def repair_line_spacing(
    lines: list[dict[str, Any]], source_text: str
) -> tuple[list[dict[str, Any]], int]:
    if not source_text or not lines:
        return lines, 0
    normalized_source = spacing_normalize(source_text)
    if not normalized_source:
        return lines, 0
    original_index: list[int] = []
    normalized_chars: list[str] = []
    for original_position, char in enumerate(source_text):
        normalized = spacing_normalize(char)
        for item in normalized:
            normalized_chars.append(item)
            original_index.append(original_position)
    normalized_source = "".join(normalized_chars)
    repaired_count = 0
    cursor = 0
    for line in lines:
        normalized_line = spacing_normalize(line["text"])
        if len(normalized_line) < 8:
            continue
        start = normalized_source.find(normalized_line, cursor)
        if start < 0:
            start = normalized_source.find(normalized_line)
        if start < 0:
            continue
        end = start + len(normalized_line) - 1
        fragment = source_text[original_index[start] : original_index[end] + 1]
        repaired = canonical_text(fragment)
        if re.sub(r"\s+", "", repaired) != re.sub(r"\s+", "", line["text"]):
            continue
        line["source_start"] = start
        line["source_end"] = end
        if repaired != line["text"]:
            line["text"] = repaired
            repaired_count += 1
        cursor = end + 1
    return lines, repaired_count


def extract_line_words(page: Any) -> list[dict[str, Any]]:
    words = page.extract_words(
        keep_blank_chars=False,
        use_text_flow=False,
        extra_attrs=["fontname", "size", "upright"],
    )
    valid = [
        word
        for word in words
        if canonical_text(word.get("text", "")) and word.get("upright", True) is not False
    ]
    valid.sort(key=lambda word: (float(word["top"]), float(word["x0"])))
    lines: list[dict[str, Any]] = []
    for word in valid:
        top = float(word["top"])
        bottom = float(word["bottom"])
        x0 = float(word["x0"])
        x1 = float(word["x1"])
        matched: dict[str, Any] | None = None
        for line in reversed(lines):
            if x0 < line["x0"] - 1.0:
                continue
            word_center = (top + bottom) / 2
            line_center = (line["top"] + line["bottom"]) / 2
            center_tolerance = max(
                1.5,
                min(bottom - top, line["bottom"] - line["top"]) * 0.55,
            )
            horizontal_gap = x0 - line["x1"]
            max_gap = max(4.0, float(page.width) * 0.012)
            if abs(word_center - line_center) <= center_tolerance and horizontal_gap <= max_gap:
                matched = line
                break
        if matched is None:
            lines.append(
                {
                    "words": [word],
                    "x0": x0,
                    "x1": x1,
                    "top": top,
                    "bottom": bottom,
                }
            )
        else:
            matched["words"].append(word)
            matched["x0"] = min(matched["x0"], x0)
            matched["x1"] = max(matched["x1"], x1)
            matched["top"] = min(matched["top"], top)
            matched["bottom"] = max(matched["bottom"], bottom)

    records: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        ordered_words = sorted(line["words"], key=lambda word: float(word["x0"]))
        text = join_words(ordered_words)
        raw_text = " ".join(
            str(word.get("text", "")).replace("\x00", "") for word in ordered_words
        )
        if not text:
            continue
        sizes = [float(word.get("size", 0.0)) for word in ordered_words if word.get("size")]
        fonts = Counter(str(word.get("fontname", "")) for word in ordered_words)
        fontname = fonts.most_common(1)[0][0] if fonts else ""
        records.append(
            {
                "raw_id": f"l{index:04d}",
                "text": text,
                "raw_text": raw_text,
                "x0": round(float(line["x0"]), 2),
                "x1": round(float(line["x1"]), 2),
                "top": round(float(line["top"]), 2),
                "bottom": round(float(line["bottom"]), 2),
                "size": round(statistics.median(sizes), 2) if sizes else None,
                "fontname": fontname,
                "bold": is_bold(fontname),
                "kind": "body",
            }
        )
    records.sort(key=lambda item: (item["top"], item["x0"]))
    return merge_heading_fragments(records, float(page.width))


def extract_rotated_text(page: Any) -> dict[str, Any] | None:
    try:
        words = page.extract_words(
            keep_blank_chars=False,
            use_text_flow=False,
            extra_attrs=["upright", "size"],
        )
    except Exception:
        return None
    rotated = [
        word
        for word in words
        if canonical_text(word.get("text", "")) and word.get("upright", True) is False
    ]
    if not rotated:
        return None
    rotated.sort(key=lambda word: (float(word["x0"]), float(word["top"])))
    text = " ".join(canonical_text(word.get("text", "")) for word in rotated)
    bbox = bbox_union(
        [[word["x0"], word["top"], word["x1"], word["bottom"]] for word in rotated]
    )
    return {"text": text, "bbox": bbox}


def merge_heading_fragments(
    records: list[dict[str, Any]], page_width: float
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index = 0
    numbering = re.compile(r"^(?:\d+(?:\.\d+){0,4}\.?|[IVXLC]+\.)$")
    while index < len(records):
        current = records[index]
        if index + 1 < len(records):
            following = records[index + 1]
            center_current = (current["top"] + current["bottom"]) / 2
            center_following = (following["top"] + following["bottom"]) / 2
            gap = following["x0"] - current["x1"]
            starts_heading = bool(
                re.match(r"^[A-Z\u3400-\u9fff]", following["text"])
            )
            if (
                numbering.fullmatch(current["text"])
                and starts_heading
                and abs(center_current - center_following) <= 2.0
                and 0 <= gap <= page_width * 0.045
            ):
                combined = current.copy()
                combined["text"] = f"{current['text']} {following['text']}"
                combined["raw_text"] = " ".join(
                    value
                    for value in (
                        current.get("raw_text", current["text"]),
                        following.get("raw_text", following["text"]),
                    )
                    if value
                )
                combined["x1"] = following["x1"]
                combined["top"] = min(current["top"], following["top"])
                combined["bottom"] = max(current["bottom"], following["bottom"])
                combined["bold"] = bool(current["bold"] or following["bold"])
                combined["fontname"] = following["fontname"] or current["fontname"]
                combined["size"] = following["size"] or current["size"]
                merged.append(combined)
                index += 2
                continue
        merged.append(current)
        index += 1
    return merged


def normalize_table_cell(value: Any) -> str:
    text = canonical_text(value)
    text = text.replace("|", "\\|")
    return re.sub(r"\s*\n\s*", " ", text)


def table_to_markdown(rows: list[list[Any]]) -> str:
    normalized = [[normalize_table_cell(cell) for cell in row] for row in rows]
    normalized = [row for row in normalized if any(cell for cell in row)]
    if not normalized:
        return ""
    width = max(len(row) for row in normalized)
    padded = [row + [""] * (width - len(row)) for row in normalized]
    header = padded[0]
    body = padded[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def extract_tables(page: Any) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    tables: list[dict[str, Any]] = []
    try:
        found = page.find_tables()
    except Exception as exc:
        return [], [f"Table detection failed: {exc}"]
    for index, table in enumerate(found, start=1):
        try:
            rows = table.extract()
        except Exception as exc:
            warnings.append(f"Table {index} could not be extracted: {exc}")
            continue
        markdown = table_to_markdown(rows)
        suspicious_cells = []
        for row_index, row in enumerate(rows):
            for column_index, cell in enumerate(row):
                lines = [
                    canonical_text(part)
                    for part in str(cell or "").splitlines()
                    if canonical_text(part)
                ]
                if len(lines) >= 3:
                    suspicious_cells.append(
                        {
                            "row": row_index + 1,
                            "column": column_index + 1,
                            "line_count": len(lines),
                        }
                    )
        table_status = "success" if markdown and not suspicious_cells else "review"
        if suspicious_cells:
            warnings.append(
                f"Table {index} may have a missed row boundary or merged cell; "
                f"inspect cells {suspicious_cells}."
            )
        tables.append(
            {
                "table_id": f"t{index:02d}",
                "bbox": bbox_round(table.bbox),
                "rows": rows,
                "markdown": markdown,
                "status": table_status,
                "review_cells": suspicious_cells,
            }
        )
    return tables, warnings


def merge_drawing_boxes(page: Any, table_boxes: list[list[float]]) -> list[dict[str, Any]]:
    boxes: list[list[float]] = []
    for obj in list(getattr(page, "images", [])) + list(getattr(page, "rects", [])) + list(
        getattr(page, "curves", [])
    ):
        box = [obj.get("x0"), obj.get("top"), obj.get("x1"), obj.get("bottom")]
        if any(value is None for value in box):
            continue
        width = float(box[2]) - float(box[0])
        height = float(box[3]) - float(box[1])
        if width < 24 or height < 24 or width * height < 1200:
            continue
        if any(intersection_area(box, table_box) > width * height * 0.65 for table_box in table_boxes):
            continue
        boxes.append([float(value) for value in box])

    merged: list[list[float]] = []
    for box in sorted(boxes, key=lambda item: (item[1], item[0])):
        assigned = False
        for current in merged:
            pad = 8.0
            if not (
                box[2] < current[0] - pad
                or box[0] > current[2] + pad
                or box[3] < current[1] - pad
                or box[1] > current[3] + pad
            ):
                current[0] = min(current[0], box[0])
                current[1] = min(current[1], box[1])
                current[2] = max(current[2], box[2])
                current[3] = max(current[3], box[3])
                assigned = True
                break
        if not assigned:
            merged.append(box.copy())
    return [
        {
            "figure_id": f"f{index:02d}",
            "bbox": bbox_round(box),
            "caption": "",
            "caption_bbox": None,
        }
        for index, box in enumerate(merged, start=1)
    ]


def page_char_count(page: Any) -> int:
    try:
        return sum(len(canonical_text(char.get("text", ""))) for char in page.chars)
    except Exception:
        return 0


def find_pdf_command(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def render_page_image(pdf_path: Path, page_number: int, output_path: Path) -> bool:
    pdftoppm = find_pdf_command("pdftoppm")
    if pdftoppm:
        prefix = output_path.with_suffix("")
        command = [
            pdftoppm,
            "-f",
            str(page_number),
            "-l",
            str(page_number),
            "-r",
            "300",
            "-png",
            str(pdf_path),
            str(prefix),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
        candidates = sorted(prefix.parent.glob(prefix.name + "*.png"))
        if completed.returncode == 0 and candidates:
            candidates[0].replace(output_path)
            return True

    try:
        import pypdfium2 as pdfium
    except ImportError:
        return False
    try:
        document = pdfium.PdfDocument(str(pdf_path))
        page = document[page_number - 1]
        bitmap = page.render(scale=300 / 72)
        image = bitmap.to_pil()
        image.save(output_path)
        return True
    except Exception:
        return False


def run_external_ocr(pdf_path: Path, page_number: int, language: str) -> tuple[str, str | None]:
    template = os.environ.get("PDF_OCR_COMMAND")
    if template:
        with tempfile.TemporaryDirectory(prefix="pdf-evidence-ocr-") as temp_dir:
            try:
                if template.lstrip().startswith("["):
                    command = json.loads(template)
                    if not isinstance(command, list) or not all(
                        isinstance(part, str) for part in command
                    ):
                        raise ValueError(
                            "PDF_OCR_COMMAND JSON must be a string argument array"
                        )
                else:
                    command = shlex.split(template, posix=os.name != "nt")
                command = [
                    part.replace("{input_pdf}", str(pdf_path))
                    .replace("{page}", str(page_number))
                    .replace("{output_dir}", temp_dir)
                    for part in command
                ]
                if not command:
                    raise ValueError("PDF_OCR_COMMAND is empty")
                completed = subprocess.run(
                    command,
                    shell=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300,
                )
            except Exception as exc:
                return "", f"OCR command could not run: {exc}"
            if completed.returncode != 0:
                return "", canonical_text(completed.stderr) or "OCR command failed"
            output_candidates = sorted(Path(temp_dir).glob("*"))
            for candidate in output_candidates:
                if candidate.is_file() and candidate.suffix.lower() in {".txt", ".md"}:
                    return canonical_text(candidate.read_text(encoding="utf-8", errors="replace")), None
            return canonical_text(completed.stdout), None

    tesseract = find_pdf_command("tesseract")
    if not tesseract:
        return "", "No OCR backend found (set PDF_OCR_COMMAND or install tesseract)"

    with tempfile.TemporaryDirectory(prefix="pdf-evidence-ocr-") as temp_dir:
        image_path = Path(temp_dir) / f"page-{page_number}.png"
        if not render_page_image(pdf_path, page_number, image_path):
            return "", "Could not render page for OCR"
        completed = subprocess.run(
            [
                tesseract,
                str(image_path),
                "stdout",
                "-l",
                language,
                "--psm",
                "3",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if completed.returncode != 0:
            return "", canonical_text(completed.stderr) or "Tesseract failed"
        return canonical_text(completed.stdout), None


def margin_line(line: dict[str, Any], page_height: float) -> bool:
    if PAGE_NUMBER_RE.fullmatch(canonical_text(line["text"])):
        return line["top"] <= page_height * 0.06 or line["bottom"] >= page_height * 0.94
    return line["top"] <= page_height * 0.09 or line["bottom"] >= page_height * 0.91


def detect_repeated_marginals(pages: list[dict[str, Any]]) -> None:
    counts: Counter[str] = Counter()
    locations: defaultdict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    pages_by_key: defaultdict[str, set[int]] = defaultdict(set)
    for page in pages:
        for line in page["lines"]:
            if line.get("ocr_page"):
                continue
            if not margin_line(line, page["height"]):
                continue
            key = normalized_margin_key(line["text"])
            if len(key) < 2:
                continue
            counts[key] += 1
            pages_by_key[key].add(page["page"])
            locations[key].append((page["page"], line))
    threshold = max(2, int(len(pages) * 0.3 + 0.999))
    repeated = {
        key
        for key, page_numbers in pages_by_key.items()
        if len(page_numbers) >= threshold
    }
    for key in repeated:
        for _, line in locations[key]:
            line["kind"] = "header_footer"


def page_layout(lines: list[dict[str, Any]], page_width: float) -> dict[str, Any]:
    eligible = [
        line
        for line in lines
        if line["kind"] in {"body", "heading", "caption", "equation", "reference", "footnote"}
    ]
    if len(eligible) < 10:
        return {"columns": 1, "mid_x": page_width / 2, "confidence": "low"}
    mid = page_width / 2
    full_width_threshold = page_width * 0.64
    left = 0
    right = 0
    spanning = 0
    for line in eligible:
        width = line["x1"] - line["x0"]
        center = (line["x0"] + line["x1"]) / 2
        if width >= full_width_threshold:
            spanning += 1
        elif center < mid * 0.96:
            left += 1
        elif center > mid * 1.04:
            right += 1
    cross_mid = sum(
        1
        for line in eligible
        if (line["x1"] - line["x0"]) < full_width_threshold
        and line["x0"] < mid * 0.86
        and line["x1"] > mid * 1.14
    )
    is_two_column = (
        left >= 3
        and right >= 3
        and cross_mid <= max(2, int(len(eligible) * 0.18))
        and (left + right) >= len(eligible) * 0.55
    )
    return {
        "columns": 2 if is_two_column else 1,
        "mid_x": round(mid, 2),
        "left_lines": left,
        "right_lines": right,
        "cross_mid_lines": cross_mid,
        "spanning_lines": spanning,
        "confidence": "high" if is_two_column and cross_mid == 0 else "medium",
    }


def looks_like_equation(text: str) -> bool:
    value = canonical_text(text)
    if not value or len(value) > 220:
        return False
    if re.match(r"^(?:encoder|decoder|note|source|table|figure|fig\.)\s*:", value, re.I):
        return False
    words = re.findall(r"[A-Za-z]{2,}", value)
    if len(words) > 14:
        return False
    operator_count = sum(value.count(symbol) for symbol in "=≈≠≤≥<>+-×÷")
    math_symbol_count = sum(value.count(symbol) for symbol in "∑∫√±∂∇αβγδθλ^_")
    if "\\" in value or "∑" in value or "∫" in value or "√" in value:
        return True
    if "=" in value:
        if re.search(r"\([^)]*\)\s*=", value):
            return True
        if math_symbol_count > 0 and operator_count >= 1:
            return True
        if len(words) <= 6 and operator_count >= 1 and not re.search(
            r"\b(?:identical|layers?|network|model|results?|table|figure)\b", value, re.I
        ):
            return True
    return False


def looks_like_heading(
    text: str,
    size: float,
    median_size: float,
    bold: bool,
) -> bool:
    value = canonical_text(text)
    if not value or len(value) > 180:
        return False
    if looks_like_equation(value):
        return False
    words = re.findall(r"[A-Za-z\u3400-\u9fff]{2,}", value)
    if REFERENCE_RE.fullmatch(value) or ABSTRACT_RE.match(value):
        return True
    if HEADING_RE.match(value):
        return (
            len(words) <= 14
            and not re.search(r"[.!?。！？]\s*$", value)
            and bool(re.search(r"[A-Za-z\u3400-\u9fff]", value))
        )
    if size >= median_size * 1.2:
        return len(value) <= 120 and len(words) <= 16
    if bold and size >= median_size * 1.05:
        return len(value) <= 90 and len(words) <= 12
    return False


def assign_line_kinds(
    pages: list[dict[str, Any]], document_median_size: float
) -> None:
    for page in pages:
        table_boxes = [table["bbox"] for table in page["tables"] if table.get("bbox")]
        figure_boxes = [figure["bbox"] for figure in page["figures"] if figure.get("bbox")]
        for line in page["lines"]:
            if line.get("ocr_page"):
                line["kind"] = "body"
                continue
            if line["kind"] in {"header_footer", "table_text"}:
                continue
            text = line["text"]
            size = float(line["size"] or document_median_size or 10.0)
            page_width = float(page["width"])
            width_ratio = (line["x1"] - line["x0"]) / page_width if page_width else 0
            normalized = normalized_margin_key(text)
            if PAGE_NUMBER_RE.fullmatch(text) and margin_line(line, page["height"]):
                line["kind"] = "header_footer"
                continue
            if CAPTION_RE.match(text) and len(text) <= 500:
                line["kind"] = "caption"
                continue
            if looks_like_equation(text):
                line["kind"] = "equation"
                continue
            if (
                line["top"] >= page["height"] * 0.78
                and size <= document_median_size * 0.93
                and size > 0
                and len(text) < 500
            ):
                line["kind"] = "footnote"
                continue
            heading_shape = looks_like_heading(
                text,
                size,
                document_median_size,
                bool(line["bold"]),
            )
            if heading_shape:
                line["kind"] = "heading"
                if REFERENCE_RE.fullmatch(text) or ABSTRACT_RE.match(text):
                    line["heading_level"] = 1
                elif size >= document_median_size * 1.45:
                    line["heading_level"] = 1
                elif HEADING_RE.match(text) and text.split()[0].count(".") >= 1:
                    line["heading_level"] = 2
                else:
                    line["heading_level"] = 2 if size >= document_median_size * 1.12 else 3
            else:
                line["kind"] = "body"
            if width_ratio > 0.9 and line["kind"] == "body":
                line["full_width"] = True
        _attach_captions(page, table_boxes, figure_boxes)


def _attach_captions(
    page: dict[str, Any],
    table_boxes: list[list[float]],
    figure_boxes: list[list[float]],
) -> None:
    captions = [line for line in page["lines"] if line["kind"] == "caption"]
    for table, box in zip(page["tables"], table_boxes):
        match = _nearest_caption(
            [caption for caption in captions if not caption.get("attached_to")],
            box,
            expected_kind="table",
        )
        if match:
            table["caption"] = match["text"]
            table["caption_evidence_id"] = match["evidence_id"]
            match["attached_to"] = table["table_id"]
    for figure, box in zip(page["figures"], figure_boxes):
        match = _nearest_caption(
            [caption for caption in captions if not caption.get("attached_to")],
            box,
            expected_kind="figure",
        )
        if match:
            figure["caption"] = match["text"]
            figure["caption_bbox"] = [match["x0"], match["top"], match["x1"], match["bottom"]]
            figure["caption_evidence_id"] = match["evidence_id"]
            match["attached_to"] = figure["figure_id"]


def _nearest_caption(
    captions: list[dict[str, Any]],
    box: list[float],
    expected_kind: str,
) -> dict[str, Any] | None:
    viable: list[tuple[float, dict[str, Any]]] = []
    x0, y0, x1, y1 = map(float, box)
    for caption in captions:
        text = canonical_text(caption["text"]).lower()
        is_table_caption = bool(re.match(r"^(?:table|tab\.?|表)\s*", text))
        is_figure_caption = bool(re.match(r"^(?:fig(?:ure)?\.?|图)\s*", text))
        if expected_kind == "table" and not is_table_caption:
            continue
        if expected_kind == "figure" and not is_figure_caption:
            continue
        cy0 = float(caption["top"])
        cy1 = float(caption["bottom"])
        horizontal_overlap = min(float(caption["x1"]), x1) - max(float(caption["x0"]), x0)
        if horizontal_overlap <= 0:
            continue
        if cy0 >= y0 - 8 and cy0 <= y1 + 50:
            distance = abs(cy0 - y1)
        elif cy1 <= y0 + 8 and cy1 >= y0 - 50:
            distance = abs(y0 - cy1)
        else:
            continue
        viable.append((distance, caption))
    return min(viable, key=lambda item: item[0])[1] if viable else None


def order_page_items(page: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in page["lines"]:
        if line["kind"] not in {"body", "heading", "caption", "equation", "reference", "footnote"}:
            continue
        if line.get("attached_to"):
            continue
        width = line["x1"] - line["x0"]
        line["full_width"] = width >= page["width"] * 0.64
        items.append({"kind": "line", "top": line["top"], "x0": line["x0"], "x1": line["x1"], "data": line})
    for table in page["tables"]:
        if table.get("bbox"):
            box = table["bbox"]
            items.append(
                {
                    "kind": "table",
                    "top": float(box[1]),
                    "x0": float(box[0]),
                    "x1": float(box[2]),
                    "data": table,
                }
            )
    for figure in page["figures"]:
        if figure.get("bbox"):
            box = figure["bbox"]
            items.append(
                {
                    "kind": "figure",
                    "top": float(box[1]),
                    "x0": float(box[0]),
                    "x1": float(box[2]),
                    "data": figure,
                }
            )

    matched_lines = [item for item in items if item["kind"] == "line" and "source_start" in item["data"]]
    use_source_order = len(matched_lines) >= max(5, int(len(items) * 0.35))
    if use_source_order and page["layout"]["columns"] == 2:
        mid = float(page["layout"]["mid_x"])
        source_sequence = sorted(
            matched_lines, key=lambda item: int(item["data"]["source_start"])
        )
        column_codes: list[int] = []
        for item in source_sequence:
            width = item["x1"] - item["x0"]
            center = (item["x0"] + item["x1"]) / 2
            if width >= page["width"] * 0.64:
                continue
            column_codes.append(0 if center < mid else 1)
        transitions = sum(
            1
            for left, right in zip(column_codes, column_codes[1:])
            if left != right
        )
        if transitions > max(2, int(len(column_codes) * 0.2)):
            use_source_order = False
    if page["layout"]["columns"] == 2:
        use_source_order = False
    if use_source_order:
        max_source = max(int(item["data"]["source_end"]) for item in matched_lines)
        for item in items:
            if item["kind"] == "line" and "source_start" in item["data"]:
                anchor = int(item["data"]["source_start"])
                item["_sort"] = (anchor, 0, item["top"])
                continue
            attached_id = None
            if item["kind"] == "table":
                attached_id = item["data"].get("table_id")
            elif item["kind"] == "figure":
                attached_id = item["data"].get("figure_id")
            caption = next(
                (
                    line
                    for line in page["lines"]
                    if attached_id and line.get("attached_to") == attached_id
                ),
                None,
            )
            if caption and "source_start" in caption:
                anchor = int(caption["source_start"]) + 0.5
            else:
                below = [
                    line_item
                    for line_item in matched_lines
                    if line_item["top"] >= item["top"] - 2
                ]
                above = [
                    line_item
                    for line_item in matched_lines
                    if line_item["top"] < item["top"] - 2
                ]
                if below:
                    anchor = min(int(entry["data"]["source_start"]) for entry in below) + 0.25
                elif above:
                    anchor = max(int(entry["data"]["source_end"]) for entry in above) + 0.25
                else:
                    anchor = (item["top"] / max(page["height"], 1)) * (max_source + 100)
            item["_sort"] = (anchor, 1, item["top"])
        items.sort(key=lambda item: item["_sort"])
        for item in items:
            item.pop("_sort", None)
        return items

    if page["layout"]["columns"] == 2:
        mid = float(page["layout"]["mid_x"])
        boundaries = sorted({
            item["top"]
            for item in items
            if (item["x1"] - item["x0"]) >= page["width"] * 0.64
        })
        for item in items:
            band = bisect.bisect_right(boundaries, item["top"])
            width = item["x1"] - item["x0"]
            center = (item["x0"] + item["x1"]) / 2
            full_width = width >= page["width"] * 0.64
            if full_width:
                suborder = 0
            elif center < mid:
                suborder = 1
            else:
                suborder = 2
            item["_sort"] = (band, suborder, item["top"], item["x0"])
    else:
        for item in items:
            item["_sort"] = (0, 0, item["top"], item["x0"])
    items.sort(key=lambda item: item["_sort"])
    for item in items:
        item.pop("_sort", None)
    return items


def smart_join(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    if left.endswith("-") and re.match(r"^[a-z]", right):
        return left + "\n" + right
    left_char = left[-1]
    right_char = right[0]
    cjk = re.compile(r"[\u3400-\u9fff]")
    if cjk.match(left_char) or cjk.match(right_char):
        return left + right
    return left + " " + right


def paragraphize_items(
    ordered_items: list[dict[str, Any]], median_gap: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blocks: list[dict[str, Any]] = []
    paragraph_lines: list[dict[str, Any]] = []
    paragraph_text = ""
    previous_line: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal paragraph_lines, paragraph_text
        if not paragraph_lines:
            return
        blocks.append(
            {
                "kind": "paragraph",
                "text": canonical_text(paragraph_text),
                "bbox": bbox_union(
                    [
                        [
                            line["x0"],
                            line["top"],
                            line["x1"],
                            line["bottom"],
                        ]
                        for line in paragraph_lines
                    ]
                ),
                "evidence_ids": [line["evidence_id"] for line in paragraph_lines],
            }
        )
        paragraph_lines = []
        paragraph_text = ""

    for item in ordered_items:
        if item["kind"] != "line":
            flush()
            previous_line = None
            blocks.append(item)
            continue
        line = item["data"]
        if line["kind"] != "body":
            flush()
            previous_line = None
            blocks.append({"kind": line["kind"], "data": line})
            continue
        if previous_line is not None:
            vertical_gap = max(0.0, float(line["top"]) - float(previous_line["bottom"]))
            column_changed = (line["x0"] < previous_line["x0"] - 5 or line["x0"] > previous_line["x0"] + 5) and abs(
                float(line["x0"]) - float(previous_line["x0"])
            ) > 18
            indent_break = (
                float(line["x0"]) > float(previous_line["x0"]) + 8
                and bool(re.search(r"[.!?。！？:：;；]$", previous_line["text"]))
            )
            if vertical_gap > max(3.0, median_gap * 1.6) or column_changed or indent_break:
                flush()
        paragraph_lines.append(line)
        paragraph_text = smart_join(paragraph_text, line["text"])
        previous_line = line
    flush()
    return blocks, paragraph_lines


def render_blocks_markdown(
    page_number: int,
    blocks: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
) -> str:
    lines = [f"## Page {page_number}", ""]
    for block in blocks:
        kind = block["kind"]
        if kind == "paragraph":
            lines.extend([block["text"], ""])
        elif kind == "heading":
            line = block["data"]
            level = min(6, max(3, 2 + int(line.get("heading_level", 3))))
            lines.extend([f"{'#' * level} {line['text']}", ""])
        elif kind == "caption":
            line = block["data"]
            lines.extend([f"**{line['text']}**", ""])
        elif kind == "equation":
            line = block["data"]
            lines.extend(["```text", line["text"], "```", ""])
        elif kind == "footnote":
            line = block["data"]
            lines.extend([f"> Footnote source: {line['text']}", ""])
        elif kind == "reference":
            line = block["data"]
            lines.extend([f"- {line['text']}", ""])
        elif kind == "table":
            table = block["data"]
            caption = table.get("caption")
            if caption:
                lines.extend([f"**{caption}**", ""])
            if table.get("markdown"):
                lines.extend([table["markdown"], ""])
            else:
                lines.extend([f"<!-- Empty table: {table['table_id']} -->", ""])
        elif kind == "figure":
            figure = block["data"]
            caption = figure.get("caption") or "Figure caption not detected"
            page_label = figure.get("page", page_number)
            lines.extend(
                [
                    f"[Figure {page_label}-{figure['figure_id']}]",
                    "",
                    f"*{caption}*",
                    "",
                ]
            )
    return "\n".join(lines).strip() + "\n"


def extract_page(
    page: Any,
    page_number: int,
    pdf_path: Path,
    ocr_mode: str,
    ocr_language: str,
    pypdf_text: str = "",
) -> dict[str, Any]:
    width = float(page.width)
    height = float(page.height)
    text_lines = extract_line_words(page)
    text_lines, spacing_repairs = repair_line_spacing(text_lines, pypdf_text)
    rotated_text = extract_rotated_text(page)
    tables, warnings = extract_tables(page)
    figure_page = page_number
    for table in tables:
        table["page"] = figure_page
        table["global_id"] = f"p{figure_page:03d}-{table['table_id']}"
    table_boxes = [table["bbox"] for table in tables if table.get("bbox")]
    table_lookup = [
        (
            table["bbox"],
            re.sub(
                r"\s+",
                "",
                unicodedata.normalize(
                    "NFKC", str(table.get("markdown") or "")
                ).casefold(),
            ),
        )
        for table in tables
        if table.get("bbox") and table.get("markdown")
    ]
    for line in text_lines:
        line_bbox = [line["x0"], line["top"], line["x1"], line["bottom"]]
        normalized_line = re.sub(
            r"\s+", "", unicodedata.normalize("NFKC", line["text"]).casefold()
        )
        if any(
            intersection_over_line(line_bbox, box) >= 0.8
            and normalized_line
            and normalized_line in normalized_markdown
            for box, normalized_markdown in table_lookup
        ):
            line["kind"] = "table_text"
    figures = merge_drawing_boxes(page, table_boxes)
    for figure in figures:
        figure["page"] = figure_page
        figure["global_id"] = f"p{figure_page:03d}-{figure['figure_id']}"
    char_count = page_char_count(page)
    ocr_used = False
    ocr_error = None
    needs_ocr = char_count < 40 or not text_lines
    if ocr_mode != "off" and needs_ocr:
        try:
            ocr_text, ocr_error = run_external_ocr(
                pdf_path, page_number, ocr_language
            )
        except subprocess.TimeoutExpired as exc:
            ocr_text, ocr_error = "", f"OCR timed out: {exc}"
        except Exception as exc:
            ocr_text, ocr_error = "", f"OCR failed: {exc}"
        if ocr_text:
            ocr_used = True
            text_lines = [
                {
                    "raw_id": "ocr0001",
                    "text": ocr_text,
                    "raw_text": ocr_text,
                    "x0": 0.0,
                    "x1": round(width, 2),
                    "top": 0.0,
                    "bottom": round(height, 2),
                    "size": None,
                    "fontname": "OCR",
                    "bold": False,
                    "kind": "body",
                    "ocr": True,
                    "ocr_page": True,
                    "coordinate_precision": "page",
                }
            ]
        elif ocr_mode == "required":
            warnings.append(ocr_error or "OCR required but produced no text")
    elif ocr_mode == "required" and char_count < 40:
        warnings.append("OCR required but no text was recovered")
    for index, line in enumerate(text_lines, start=1):
        line["raw_id"] = f"l{index:04d}"
        line["page"] = page_number
        line["evidence_id"] = f"p{page_number:03d}-{line['raw_id']}"
        line["bbox"] = bbox_round([line["x0"], line["top"], line["x1"], line["bottom"]])
        line.setdefault("coordinate_precision", "line")
    return {
        "page": page_number,
        "width": round(width, 2),
        "height": round(height, 2),
        "char_count": char_count,
        "ocr_used": ocr_used,
        "ocr_error": ocr_error,
        "spacing_repairs": spacing_repairs,
        "lines": text_lines,
        "tables": tables,
        "figures": figures,
        "rotated_text": rotated_text,
        "warnings": warnings,
    }


def detect_reference_mode(pages: list[dict[str, Any]]) -> None:
    reference_mode = False
    reference_level = 99
    for page in pages:
        for item in page["ordered_items"]:
            if item["kind"] != "line":
                continue
            line = item["data"]
            if line["kind"] == "heading":
                level = int(line.get("heading_level", 3))
                if REFERENCE_RE.fullmatch(line["text"]):
                    reference_mode = True
                    reference_level = level
                elif reference_mode:
                    reference_mode = False
                continue
            if reference_mode and line["kind"] in {"body", "footnote", "equation"}:
                line["kind"] = "reference"


def choose_title(pages: list[dict[str, Any]]) -> str:
    if not pages:
        return ""
    first = pages[0]
    ordered = [
        item["data"]
        for item in first["ordered_items"]
        if item["kind"] == "line"
        and item["data"]["kind"] in {"heading", "body"}
        and item["data"]["top"] < first["height"] * 0.35
    ]
    if not ordered:
        return ""
    largest = max(float(line.get("size") or 0.0) for line in ordered)
    candidates = [
        line
        for line in ordered
        if float(line.get("size") or 0.0) >= largest - 0.5 and len(line["text"]) >= 3
    ]
    candidates.sort(key=lambda line: (line["top"], line["x0"]))
    title_lines = [candidates[0]["text"]] if candidates else []
    if candidates:
        previous_top = float(candidates[0]["bottom"])
        for line in candidates[1:]:
            if float(line["top"]) - previous_top <= 30 and len(" ".join(title_lines)) < 300:
                title_lines.append(line["text"])
                previous_top = float(line["bottom"])
            else:
                break
    return canonical_text(" ".join(title_lines))


def collect_metadata(pages: list[dict[str, Any]]) -> dict[str, Any]:
    title = choose_title(pages)
    first_page_text = "\n".join(
        line["text"]
        for page in pages[:1]
        for line in page["lines"]
        if line["kind"] not in {"header_footer"}
    )
    doi_match = DOI_RE.search(first_page_text)
    arxiv_match = ARXIV_RE.search(first_page_text)
    abstract = ""
    abstract_started = False
    abstract_lines: list[str] = []
    for page in pages:
        for item in page["ordered_items"]:
            if item["kind"] != "line":
                continue
            line = item["data"]
            if ABSTRACT_RE.match(line["text"]):
                abstract_started = True
                remainder = ABSTRACT_RE.sub("", line["text"]).strip()
                if remainder:
                    abstract_lines.append(remainder)
                continue
            if abstract_started and line["kind"] == "heading":
                abstract_started = False
                if abstract_lines:
                    break
                continue
            if abstract_started:
                abstract_lines.append(line["text"])
        if abstract_lines and not abstract_started:
            break
        if abstract_lines and page["page"] >= 2:
            break
    abstract = canonical_text(" ".join(abstract_lines))
    sections = [
        {
            "page": page["page"],
            "text": line["text"],
            "level": int(line.get("heading_level", 3)),
            "bbox": line["bbox"],
        }
        for page in pages
        for item in page["ordered_items"]
        if item["kind"] == "line"
        for line in [item["data"]]
        if line["kind"] == "heading"
    ]
    return {
        "title": title,
        "abstract": abstract,
        "doi": doi_match.group(1).rstrip(".,;") if doi_match else None,
        "arxiv": arxiv_match.group(1) if arxiv_match else None,
        "sections": sections,
    }


def contract_keys(contract: Any) -> list[str]:
    if isinstance(contract, dict):
        if isinstance(contract.get("properties"), dict):
            keys = list(contract["properties"])
            required = contract.get("required", [])
            if isinstance(required, list):
                keys.extend(required)
            return list(dict.fromkeys(str(key) for key in keys))
        return [str(key) for key in contract.keys()]
    if isinstance(contract, list):
        keys: list[str] = []
        for item in contract:
            if isinstance(item, str):
                keys.append(item)
            elif isinstance(item, dict):
                for key in ("name", "field", "key", "id"):
                    if item.get(key):
                        keys.append(str(item[key]))
                        break
        return keys
    return []


def map_contract_value(
    key: str, document: dict[str, Any], evidence: list[dict[str, Any]]
) -> Any:
    normalized = re.sub(r"[^a-z0-9\u3400-\u9fff]+", "_", key.lower()).strip("_")
    exact = {
        "case_id": document.get("case_id"),
        "page_count": document["page_count"],
        "number_of_pages": document["page_count"],
        "title": document["metadata"]["title"],
        "paper_title": document["metadata"]["title"],
        "abstract": document["metadata"]["abstract"],
        "doi": document["metadata"]["doi"],
        "arxiv": document["metadata"]["arxiv"],
        "authors": None,
        "metadata": document["metadata"],
        "equations": document.get("formulas", []),
        "transcription": document["full_markdown"],
        "正文": document["full_markdown"],
        "全文": document["full_markdown"],
        "表格": [table for page in document["pages"] for table in page["tables"]],
        "公式": document.get("formulas", []),
        "参考文献": document["references"],
        "证据": evidence,
        "abstract_text": document["metadata"]["abstract"],
        "markdown": document["full_markdown"],
        "full_text": document["full_markdown"],
        "full_markdown": document["full_markdown"],
        "pages": document["pages"],
        "tables": [table for page in document["pages"] for table in page["tables"]],
        "figures": [figure for page in document["pages"] for figure in page["figures"]],
        "formulas": document.get("formulas", []),
        "headings": document["metadata"]["sections"],
        "sections": document["metadata"]["sections"],
        "table_of_contents": document["metadata"]["sections"],
        "references": document["references"],
        "evidence": evidence,
        "sources": evidence,
        "uncertainties": document["uncertainties"],
        "status": document["status"],
    }
    return exact.get(normalized)


def build_contract_output(
    case_data: dict[str, Any],
    document: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    contract = (
        case_data.get("output_contract")
        or case_data.get("outputContract")
        or case_data.get("output")
        or {}
    )
    keys = contract_keys(contract)
    issues: list[str] = []
    output: dict[str, Any] = {}
    for key in keys:
        value = map_contract_value(key, document, evidence)
        output[key] = value
        if value is None and key not in {"case_id"}:
            issues.append(
                f"Output contract field {key!r} has no exact mapping and requires case-specific completion."
            )
    if not keys:
        issues.append("No output_contract keys were found in case.json; inspect task.md.")
    return output, issues


def find_pdf_references(value: Any, base_dir: Path) -> list[Path]:
    found: list[Path] = []
    if isinstance(value, str):
        if value.lower().endswith(".pdf"):
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = base_dir / candidate
            found.append(candidate)
    elif isinstance(value, list):
        for item in value:
            found.extend(find_pdf_references(item, base_dir))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(find_pdf_references(item, base_dir))
    return found


def resolve_pdf(
    pdf_override: str | None,
    case_path: Path | None,
    case_data: dict[str, Any],
) -> Path:
    if pdf_override:
        path = Path(pdf_override).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"PDF not found: {path}")
        return path
    base_dir = case_path.parent if case_path else Path.cwd()
    referenced = [path for path in find_pdf_references(case_data, base_dir) if path.is_file()]
    if len(referenced) == 1:
        return referenced[0].resolve()
    search_roots = [base_dir, base_dir / "input", base_dir.parent / "input"]
    candidates: list[Path] = []
    for root in search_roots:
        if root.is_dir():
            candidates.extend(sorted(root.glob("*.pdf")))
    unique = sorted({path.resolve() for path in candidates if path.is_file()})
    if len(unique) == 1:
        return unique[0]
    if len(referenced) > 1:
        raise ValueError(
            "case.json references multiple PDFs; pass --pdf to select the intended file: "
            + ", ".join(str(path) for path in referenced)
        )
    if len(unique) > 1:
        raise ValueError(
            "Multiple PDFs were found; pass --pdf to select the intended file: "
            + ", ".join(str(path) for path in unique)
        )
    raise FileNotFoundError("No input PDF found. Pass --pdf or reference it in case.json.")


def load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    if not path.is_file():
        raise FileNotFoundError(f"Case file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def attach_evidence(
    pages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    formulas: list[dict[str, Any]] = []
    for page in pages:
        for line in page["lines"]:
            if line["kind"] == "header_footer":
                continue
            item = {
                "evidence_id": line["evidence_id"],
                "page": page["page"],
                "bbox": line["bbox"],
                "kind": line["kind"],
                "text": line["text"],
                "raw_text": line.get("raw_text", line["text"]),
                "locator": f"page {page['page']}, bbox {line['bbox']}",
                "ocr": bool(line.get("ocr")),
                "coordinate_precision": line.get("coordinate_precision", "line"),
            }
            evidence.append(item)
            if line["kind"] == "equation":
                formula = {
                    "formula_id": f"eq-p{page['page']:03d}-{line['raw_id']}",
                    "page": page["page"],
                    "bbox": line["bbox"],
                    "text": line["text"],
                    "evidence_id": line["evidence_id"],
                    "status": "text-level extraction; semantic interpretation not attempted",
                }
                formulas.append(formula)
        for table in page["tables"]:
            evidence_id = f"p{page['page']:03d}-{table['table_id']}"
            table["evidence_id"] = evidence_id
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "page": page["page"],
                    "bbox": table.get("bbox"),
                    "kind": "table",
                    "text": table.get("markdown", ""),
                    "locator": f"page {page['page']}, table bbox {table.get('bbox')}",
                    "coordinate_precision": "region",
                }
            )
        for figure in page["figures"]:
            evidence_id = f"p{page['page']:03d}-{figure['figure_id']}"
            figure["evidence_id"] = evidence_id
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "page": page["page"],
                    "bbox": figure.get("bbox"),
                    "kind": "figure",
                    "text": figure.get("caption", ""),
                    "locator": f"page {page['page']}, figure bbox {figure.get('bbox')}",
                    "coordinate_precision": "region",
                }
            )
        if page.get("rotated_text"):
            evidence.append(
                {
                    "evidence_id": f"p{page['page']:03d}-rotated",
                    "page": page["page"],
                    "bbox": page["rotated_text"].get("bbox"),
                    "kind": "rotated_text",
                    "text": page["rotated_text"].get("text", ""),
                    "locator": f"page {page['page']}, rotated text bbox {page['rotated_text'].get('bbox')}",
                    "coordinate_precision": "region",
                }
            )
    return evidence, formulas


def add_uncertainties(
    pages: list[dict[str, Any]],
    metadata: dict[str, Any],
    contract_issues: list[str],
) -> list[dict[str, Any]]:
    uncertainties: list[dict[str, Any]] = []
    for page in pages:
        if page["ocr_used"]:
            uncertainties.append(
                {
                    "page": page["page"],
                    "scope": "ocr",
                    "reason": "Page-level OCR was used for the whole page.",
                    "impact": "Paragraph boundaries and reading order require visual review.",
                }
            )
        if page["ocr_error"]:
            uncertainties.append(
                {
                    "page": page["page"],
                    "scope": "ocr",
                    "reason": page["ocr_error"],
                    "impact": "Text on this page may be missing or incomplete.",
                }
            )
        if page["char_count"] < 20 and not page["ocr_used"]:
            uncertainties.append(
                {
                    "page": page["page"],
                    "scope": "text_layer",
                    "reason": "Very little machine-readable text was found.",
                    "impact": "Treat page extraction as partial.",
                }
            )
        for warning in page["warnings"]:
            uncertainties.append(
                {
                    "page": page["page"],
                    "scope": "layout",
                    "reason": warning,
                    "impact": "Review tables or structure against the page image.",
                }
            )
    if not metadata.get("title"):
        uncertainties.append(
            {
                "page": 1,
                "scope": "metadata",
                "reason": "Title was not confidently detected.",
                "impact": "Use the first large-font heading after visual review.",
            }
        )
    for issue in contract_issues:
        uncertainties.append(
            {
                "page": None,
                "scope": "output_contract",
                "reason": issue,
                "impact": "Final task-specific fields may need manual completion.",
            }
        )
    return uncertainties


def locate_case_and_task(args: argparse.Namespace) -> tuple[Path | None, Path | None]:
    case_path = Path(args.case).expanduser().resolve() if args.case else None
    task_path = Path(args.task).expanduser().resolve() if args.task else None
    if case_path is None:
        default_case = Path("/input/case.json")
        if default_case.is_file():
            case_path = default_case
    if task_path is None:
        if case_path and (case_path.parent / "task.md").is_file():
            task_path = case_path.parent / "task.md"
        elif Path("/input/task.md").is_file():
            task_path = Path("/input/task.md")
    return case_path, task_path


def parse_document(args: argparse.Namespace) -> dict[str, Any]:
    if pdfplumber is None:
        raise RuntimeError(
            "pdfplumber is not installed; install the packages in requirements.txt"
        )
    case_path, task_path = locate_case_and_task(args)
    case_data = load_json(case_path)
    task_text = task_path.read_text(encoding="utf-8", errors="replace") if task_path else ""
    pdf_path = resolve_pdf(args.pdf, case_path, case_data)
    started_at = utc_now()
    warnings: list[str] = []
    pages: list[dict[str, Any]] = []
    pdf_metadata: dict[str, Any] = {}
    encrypted = False
    source_page_count = 0

    with pdfplumber.open(pdf_path) as pdf:
        pdf_metadata = {str(key): str(value) for key, value in (pdf.metadata or {}).items()}
        encrypted = bool(getattr(pdf, "is_encrypted", False))
        source_page_count = len(pdf.pages)
        if args.max_pages is not None:
            selected_pages = pdf.pages[: args.max_pages]
            if len(selected_pages) < source_page_count:
                warnings.append(
                    f"Only {len(selected_pages)} of {source_page_count} pages were processed by --max-pages."
                )
        else:
            selected_pages = pdf.pages
        pypdf_pages: list[Any] = []
        if PdfReader is not None:
            try:
                pypdf_pages = list(PdfReader(str(pdf_path)).pages)
            except Exception as exc:
                warnings.append(f"pypdf spacing repair unavailable: {exc}")
        for page_number, page in enumerate(selected_pages, start=1):
            try:
                spacing_text = ""
                if page_number <= len(pypdf_pages):
                    try:
                        spacing_text = pypdf_pages[page_number - 1].extract_text() or ""
                    except Exception as exc:
                        warnings.append(
                            f"pypdf text extraction failed on page {page_number}: {exc}"
                        )
                pages.append(
                    extract_page(
                        page,
                        page_number,
                        pdf_path,
                        args.ocr,
                        args.ocr_language,
                        spacing_text,
                    )
                )
            except Exception as exc:
                warnings.append(f"Page {page_number} failed: {exc}")
                pages.append(
                    {
                        "page": page_number,
                        "width": round(float(page.width), 2),
                        "height": round(float(page.height), 2),
                        "char_count": 0,
                        "spacing_repairs": 0,
                        "ocr_used": False,
                        "ocr_error": str(exc),
                        "lines": [],
                        "tables": [],
                        "figures": [],
                        "rotated_text": None,
                        "warnings": [str(exc)],
                    }
                )

    if encrypted:
        warnings.append("PDF reports an encrypted/permission-restricted state.")
    if not pages:
        raise ValueError("The PDF contains no readable pages.")

    detect_repeated_marginals(pages)
    median_sizes = [
        float(line["size"])
        for page in pages
        for line in page["lines"]
        if line.get("size") and len(line["text"]) >= 8
    ]
    document_median_size = statistics.median(median_sizes) if median_sizes else 10.0
    assign_line_kinds(pages, document_median_size)

    for page in pages:
        page["layout"] = page_layout(page["lines"], page["width"])
        page["ordered_items"] = order_page_items(page)
        gaps = [
            max(0.0, float(page["ordered_items"][index]["top"]) - float(page["ordered_items"][index - 1]["top"]))
            for index in range(1, len(page["ordered_items"]))
            if page["ordered_items"][index]["kind"] == "line"
            and page["ordered_items"][index - 1]["kind"] == "line"
        ]
        page["median_line_gap"] = round(statistics.median(gaps), 2) if gaps else 12.0

    detect_reference_mode(pages)
    evidence, formulas = attach_evidence(pages)
    evidence_by_id = {item["evidence_id"]: item for item in evidence}

    markdown_parts: list[str] = []
    for page in pages:
        blocks, _ = paragraphize_items(page["ordered_items"], page["median_line_gap"])
        page["blocks"] = blocks
        markdown_parts.append(
            render_blocks_markdown(page["page"], blocks, evidence_by_id)
        )
    full_markdown = "\n".join(markdown_parts).strip() + "\n"
    metadata = collect_metadata(pages)
    references = [
        {
            "page": page["page"],
            "text": line["text"],
            "bbox": line["bbox"],
            "evidence_id": line["evidence_id"],
        }
        for page in pages
        for item in page["ordered_items"]
        for line in [item["data"]]
        if item["kind"] == "line"
        if line["kind"] == "reference"
    ]

    public_pages = []
    for page in pages:
        public_pages.append(
            {
                "page": page["page"],
                "width": page["width"],
                "height": page["height"],
                "char_count": page["char_count"],
                "spacing_repairs": page["spacing_repairs"],
                "ocr_used": page["ocr_used"],
                "ocr_error": page["ocr_error"],
                "layout": page["layout"],
                "lines": page["lines"],
                "tables": page["tables"],
                "figures": page["figures"],
                "rotated_text": page.get("rotated_text"),
                "blocks": page.get("blocks", []),
                "warnings": page["warnings"],
            }
        )
    low_text_pages = [
        page["page"]
        for page in pages
        if page["char_count"] < 20 and not page["ocr_used"]
    ]
    tables_need_review = any(
        table.get("status") in {"review", "empty"}
        for page in pages
        for table in page["tables"]
    )
    page_warnings = any(page["warnings"] for page in pages)
    status = "success"
    if (
        low_text_pages
        or warnings
        or page_warnings
        or tables_need_review
        or any(page["ocr_used"] for page in pages)
        or any(page["ocr_error"] for page in pages)
    ):
        status = "partial"

    extraction_status = status
    document = {
        "case_id": case_data.get("case_id") or case_data.get("caseId") or case_data.get("id"),
        "status": status,
        "page_count": len(pages),
        "metadata": metadata,
        "full_markdown": full_markdown,
        "references": references,
        "pages": public_pages,
        "formulas": formulas,
        "uncertainties": [],
    }
    contract_output, contract_issues = build_contract_output(case_data, document, evidence)
    uncertainties = add_uncertainties(pages, metadata, contract_issues)
    if contract_issues and status == "success":
        status = "partial"
    has_structure = any(page["tables"] or page["figures"] for page in public_pages) or bool(
        formulas
    )
    if not evidence and not has_structure:
        status = "failure"
    document["status"] = status
    document["uncertainties"] = uncertainties
    contract_output, _ = build_contract_output(case_data, document, evidence)
    if formulas:
        for page in public_pages:
            page["formulas"] = [
                formula for formula in formulas if formula["page"] == page["page"]
            ]
    result = {
        **contract_output,
        "schema_version": "scientific-pdf-evidence-reader/v1",
        "case_id": case_data.get("case_id") or case_data.get("caseId") or case_data.get("id"),
        "input_identifier": {
            "filename": pdf_path.name,
            "sha256": file_sha256(pdf_path),
            "page_count": source_page_count,
            "processed_page_count": len(pages),
        },
        "task": {
            "case_path": str(case_path) if case_path else None,
            "task_path": str(task_path) if task_path else None,
            "task_markdown": task_text,
        },
        "status": status,
        "document": {
            "metadata": metadata,
            "page_count": len(pages),
            "full_markdown": full_markdown,
            "references": references,
            "pages": public_pages,
            "formulas": formulas,
            "blocks": [
                block
                for page in public_pages
                for block in page.get("blocks", [])
            ],
        },
        "contract_output": contract_output,
        "evidence": evidence,
        "uncertainties": uncertainties,
        "change_records": [
            {
                "at": started_at,
                "action": "parse_pdf",
                "detail": "Extracted text, layout, tables, figures, equations and page coordinates.",
            },
            {
                "at": utc_now(),
                "action": "finalize_result",
                "detail": f"Status {status}; {len(evidence)} evidence records.",
            },
        ],
        "runtime": {
            "parser": "pdfplumber",
            "pdf_metadata": pdf_metadata,
            "encrypted": encrypted,
            "ocr_mode": args.ocr,
            "ocr_language": args.ocr_language,
            "warnings": warnings,
            "extraction_status": (
                "failure" if status == "failure" else extraction_status
            ),
            "source_page_count": source_page_count,
            "processed_page_count": len(pages),
        },
    }
    result["contract_output"] = {
        key: result.get(key) for key in contract_output
    }
    return result


def write_artifacts(result: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, result)
    write_text(output_path.parent / "document.md", result["document"]["full_markdown"])
    write_json(output_path.parent / "pages.json", result["document"]["pages"])
    write_json(output_path.parent / "tables.json", [
        table
        for page in result["document"]["pages"]
        for table in page["tables"]
    ])
    evidence_text = "\n".join(
        json.dumps(item, ensure_ascii=False)
        for item in result["evidence"]
    ) + "\n"
    write_text(output_path.parent / "evidence.jsonl", evidence_text)
    write_text(
        output_path.parent / "run.log",
        "\n".join(
            [
                f"started={result['change_records'][0]['at']}",
                f"finished={result['change_records'][-1]['at']}",
                f"status={result['status']}",
                f"pages={result['document']['page_count']}",
                f"evidence={len(result['evidence'])}",
                f"uncertainties={len(result['uncertainties'])}",
            ]
        )
        + "\n",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract evidence-linked scientific PDF structure."
    )
    parser.add_argument("--case", help="Path to /input/case.json")
    parser.add_argument("--task", help="Path to task.md")
    parser.add_argument("--pdf", help="Explicit PDF path")
    parser.add_argument(
        "--out",
        default="/app/output/result.json",
        help="Output JSON path (default: /app/output/result.json)",
    )
    parser.add_argument(
        "--ocr",
        choices=("auto", "off", "required"),
        default="auto",
        help="OCR policy for pages with little or no text layer",
    )
    parser.add_argument(
        "--ocr-language",
        default=os.environ.get("PDF_OCR_LANGUAGE", "eng+chi_sim"),
        help="OCR language string",
    )
    parser.add_argument("--max-pages", type=positive_int, default=None)
    return parser


def build_failure_result(args: argparse.Namespace, exc: Exception) -> dict[str, Any]:
    case_path, task_path = locate_case_and_task(args)
    case_data: dict[str, Any] = {}
    case_error = None
    try:
        case_data = load_json(case_path)
    except Exception as error:
        case_error = str(error)
    task_text = ""
    try:
        if task_path and task_path.is_file():
            task_text = task_path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        detail = f"Could not read task while reporting failure: {error}"
        case_error = f"{case_error}; {detail}" if case_error else detail
    contract = (
        case_data.get("output_contract")
        or case_data.get("outputContract")
        or case_data.get("output")
        or {}
    )
    contract_fields = {key: None for key in contract_keys(contract)}
    uncertainty = {
        "page": None,
        "scope": "runtime",
        "reason": str(exc),
        "impact": "No usable document result was produced.",
    }
    result = {
        **contract_fields,
        "schema_version": "scientific-pdf-evidence-reader/v1",
        "case_id": case_data.get("case_id") or case_data.get("caseId") or case_data.get("id"),
        "status": "failure",
        "input_identifier": {
            "filename": Path(args.pdf).name if args.pdf else None,
            "sha256": None,
            "page_count": 0,
            "processed_page_count": 0,
        },
        "task": {
            "case_path": str(case_path) if case_path else None,
            "task_path": str(task_path) if task_path else None,
            "task_markdown": task_text,
        },
        "document": {
            "metadata": {
                "title": "",
                "abstract": "",
                "doi": None,
                "arxiv": None,
                "sections": [],
            },
            "page_count": 0,
            "full_markdown": "",
            "references": [],
            "pages": [],
            "formulas": [],
            "blocks": [],
            "uncertainties": [uncertainty],
        },
        "contract_output": contract_fields,
        "evidence": [],
        "uncertainties": [uncertainty],
        "change_records": [
            {
                "at": utc_now(),
                "action": "failure",
                "detail": str(exc),
            }
        ],
        "runtime": {
            "parser": "pdfplumber",
            "encrypted": False,
            "ocr_mode": args.ocr,
            "ocr_language": args.ocr_language,
            "warnings": [value for value in [case_error] if value],
            "source_page_count": 0,
            "processed_page_count": 0,
            "traceback": traceback.format_exc(),
        },
    }
    result["contract_output"] = {
        key: result.get(key) for key in contract_fields
    }
    return result


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = parse_document(args)
        write_artifacts(result, Path(args.out).expanduser().resolve())
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "output": str(Path(args.out).expanduser().resolve()),
                    "pages": result["document"]["page_count"],
                    "evidence": len(result["evidence"]),
                    "uncertainties": len(result["uncertainties"]),
                },
                ensure_ascii=False,
            )
        )
        return 0 if result["status"] != "failure" else 2
    except Exception as exc:
        output_path = Path(args.out).expanduser().resolve()
        failure = build_failure_result(args, exc)
        write_artifacts(failure, output_path)
        print(
            json.dumps(
                {
                    "status": "failure",
                    "error": str(exc),
                    "output": str(output_path),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
