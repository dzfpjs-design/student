from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from pypdf import PdfReader


MOE_PAGE_URL = "http://www.moe.gov.cn/srcsite/A26/s8001/202204/t20220420_619921.html"
STANDARD_METADATA = {
    "curriculum_plan.pdf": ("课程方案", "义务教育课程方案（2022年版）"),
    "morality_law.pdf": ("道德与法治", "义务教育道德与法治课程标准（2022年版）"),
    "chinese.pdf": ("语文", "义务教育语文课程标准（2022年版）"),
    "history.pdf": ("历史", "义务教育历史课程标准（2022年版）"),
    "mathematics.pdf": ("数学", "义务教育数学课程标准（2022年版）"),
    "english.pdf": ("英语", "义务教育英语课程标准（2022年版）"),
    "japanese.pdf": ("日语", "义务教育日语课程标准（2022年版）"),
    "russian.pdf": ("俄语", "义务教育俄语课程标准（2022年版）"),
    "geography.pdf": ("地理", "义务教育地理课程标准（2022年版）"),
    "science.pdf": ("科学", "义务教育科学课程标准（2022年版）"),
    "physics.pdf": ("物理", "义务教育物理课程标准（2022年版）"),
    "chemistry.pdf": ("化学", "义务教育化学课程标准（2022年版）"),
    "biology.pdf": ("生物学", "义务教育生物学课程标准（2022年版）"),
    "information_technology.pdf": ("信息科技", "义务教育信息科技课程标准（2022年版）"),
    "physical_health.pdf": ("体育与健康", "义务教育体育与健康课程标准（2022年版）"),
    "arts.pdf": ("艺术", "义务教育艺术课程标准（2022年版）"),
    "labour.pdf": ("劳动", "义务教育劳动课程标准（2022年版）"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract(root: Path) -> dict:
    sources = []
    for filename, (subject, title) in STANDARD_METADATA.items():
        pdf_path = root / filename
        if not pdf_path.is_file():
            raise FileNotFoundError(f"missing official standard: {pdf_path}")
        reader = PdfReader(pdf_path)
        text_path = pdf_path.with_suffix(".txt")
        pages_path = pdf_path.with_suffix(".pages.jsonl")
        full_text: list[str] = []
        with pages_path.open("w", encoding="utf-8") as output:
            for page_number, page in enumerate(reader.pages, start=1):
                text = (page.extract_text() or "").replace("\x00", "").strip()
                full_text.append(f"\n\n===== 第{page_number}页 =====\n{text}")
                output.write(json.dumps(
                    {"page": page_number, "text": text}, ensure_ascii=False
                ) + "\n")
        text_path.write_text("".join(full_text).strip() + "\n", encoding="utf-8")
        sources.append({
            "source_key": f"moe-2022-{pdf_path.stem}",
            "jurisdiction": "CN",
            "source_type": "national_standard" if subject != "课程方案" else "national_curriculum_plan",
            "subject": subject,
            "title": title,
            "version": "2022",
            "official_page_url": MOE_PAGE_URL,
            "pdf_filename": filename,
            "text_filename": text_path.name,
            "pages_filename": pages_path.name,
            "page_count": len(reader.pages),
            "pdf_sha256": sha256(pdf_path),
            "text_sha256": sha256(text_path),
        })
        print(f"extracted {filename}: {len(reader.pages)} pages")
    manifest = {
        "format": "hsc_official_curriculum_sources_v1",
        "publisher": "中华人民共和国教育部",
        "sources": sources,
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"manifest": str(manifest_path), "source_count": len(sources)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(extract(args.root.expanduser().resolve()), ensure_ascii=False))


if __name__ == "__main__":
    main()
