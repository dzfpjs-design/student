from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def run_ocr(root: Path, subjects: set[str] | None) -> None:
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    project_root = Path(__file__).resolve().parents[1]
    swift_source = project_root / "scripts" / "ocr-standard-page.swift"
    poppler = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/pdftoppm"
    if not poppler.is_file():
        fallback = shutil.which("pdftoppm")
        if not fallback:
            raise RuntimeError("pdftoppm is unavailable")
        poppler = Path(fallback)

    with tempfile.TemporaryDirectory(prefix="hsc-standard-ocr-") as temp_value:
        temp = Path(temp_value)
        ocr_binary = temp / "ocr-standard-page"
        subprocess.run(["swiftc", str(swift_source), "-o", str(ocr_binary)], check=True)
        for source in manifest["sources"]:
            if subjects and source["subject"] not in subjects:
                continue
            pdf_filename = source.get("pdf_filename")
            if not pdf_filename:
                print(f"{source['subject']}: skip non-PDF official source", flush=True)
                continue
            pdf_path = root / pdf_filename
            pages_path = root / f"{pdf_path.stem}.ocr.pages.jsonl"
            text_path = root / f"{pdf_path.stem}.ocr.txt"
            completed: dict[int, dict] = {}
            if pages_path.is_file():
                for line in pages_path.read_text(encoding="utf-8").splitlines():
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item.get("page"), int) and isinstance(item.get("text"), str):
                        completed[item["page"]] = item
            total = int(source["page_count"])
            print(f"{source['subject']}: resume {len(completed)}/{total}", flush=True)
            for start in range(1, total + 1, 10):
                needed = [page for page in range(start, min(start + 10, total + 1)) if page not in completed]
                if not needed:
                    continue
                chunk = temp / f"{pdf_path.stem}-{start}"
                chunk.mkdir()
                subprocess.run([
                    str(poppler), "-f", str(needed[0]), "-l", str(needed[-1]),
                    "-r", "170", "-jpeg", "-jpegopt", "quality=88",
                    str(pdf_path), str(chunk / "page"),
                ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                images = sorted(chunk.glob("page-*.jpg"))
                if len(images) != needed[-1] - needed[0] + 1:
                    raise RuntimeError(f"rendered page count mismatch for {pdf_path.name}")
                for page, image in zip(range(needed[0], needed[-1] + 1), images):
                    if page in completed:
                        continue
                    result = subprocess.run(
                        [str(ocr_binary), str(image)], check=True, capture_output=True, text=True
                    )
                    text = result.stdout.replace("\x00", "").strip()
                    completed[page] = {"page": page, "text": text, "char_count": len(text)}
                    with pages_path.open("w", encoding="utf-8") as output:
                        for number in sorted(completed):
                            output.write(json.dumps(completed[number], ensure_ascii=False) + "\n")
                shutil.rmtree(chunk)
                print(f"{source['subject']}: {len(completed)}/{total}", flush=True)
            if len(completed) != total:
                raise RuntimeError(f"OCR incomplete for {source['subject']}")
            text_path.write_text("\n\n".join(
                f"===== 第{page}页 =====\n{completed[page]['text']}" for page in range(1, total + 1)
            ) + "\n", encoding="utf-8")
            source["ocr_status"] = "completed"
            source["ocr_pages_filename"] = pages_path.name
            source["ocr_text_filename"] = text_path.name
            source["ocr_char_count"] = sum(item["char_count"] for item in completed.values())
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"{source['subject']}: complete {source['ocr_char_count']} chars", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--subjects", help="comma-separated subject names")
    args = parser.parse_args()
    subjects = {value.strip() for value in (args.subjects or "").split(",") if value.strip()} or None
    run_ocr(args.root.expanduser().resolve(), subjects)


if __name__ == "__main__":
    main()
