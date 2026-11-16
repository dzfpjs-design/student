from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen


SOURCES = [
    {
        "source_key": "jiangsu-2024-textbook-selection-rules",
        "source_type": "jiangsu_textbook_catalog",
        "subject": "教材管理",
        "title": "江苏省中小学教材选用管理实施细则",
        "version": "2024-2029",
        "official_page_url": "https://jyt.jiangsu.gov.cn/art/2024/1/18/art_88010_11138172.html",
    },
    {
        "source_key": "jiangsu-2022-experiment-teaching",
        "source_type": "jiangsu_implementation",
        "subject": "实验教学",
        "title": "江苏省教育厅关于加强和改进中小学实验教学的实施意见",
        "version": "2022",
        "official_page_url": "http://jyt.jiangsu.gov.cn/art/2022/1/17/art_77617_10343078.html",
    },
    {
        "source_key": "jiangsu-2022-curriculum-implementation-evidence",
        "source_type": "jiangsu_implementation",
        "subject": "课程实施",
        "title": "江苏省实施义务教育课程方案和课程标准的官方实施说明",
        "version": "2022",
        "official_page_url": "https://jyt.jiangsu.gov.cn/art/2022/9/16/art_87153_10606584.html",
    },
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def add_sources(root: Path) -> None:
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing = {item["source_key"]: item for item in manifest["sources"]}
    for item in SOURCES:
        stem = item["source_key"]
        text_path = root / f"{stem}.txt"
        pages_path = root / f"{stem}.pages.jsonl"
        request = Request(
            f"https://r.jina.ai/{item['official_page_url']}",
            headers={"User-Agent": "HomeStudyCurriculumResearch/1.0"},
        )
        with urlopen(request, timeout=60) as response:
            content = response.read().decode("utf-8").strip()
        if len(content) < 500:
            raise RuntimeError(f"official page extraction too short: {item['official_page_url']}")
        text_path.write_text(content + "\n", encoding="utf-8")
        pages_path.write_text(json.dumps(
            {"page": 1, "text": content, "char_count": len(content)}, ensure_ascii=False
        ) + "\n", encoding="utf-8")
        existing[stem] = {
            **item,
            "jurisdiction": "JS",
            "content_filename": text_path.name,
            "content_sha256": sha256(text_path),
            "page_count": 1,
            "ocr_status": "completed",
            "pages_filename": pages_path.name,
        }
        print(f"added {item['title']}: {len(content)} chars")
    manifest["sources"] = list(existing.values())
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    add_sources(args.root.expanduser().resolve())


if __name__ == "__main__":
    main()
