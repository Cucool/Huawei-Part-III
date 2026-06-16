#!/usr/bin/env python3
from __future__ import annotations

import json
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
QA_DIR = BASE_DIR / "QA"
OUTPUT_DIR = BASE_DIR / "generated_qa_rule_cases"
OUTPUT_FILE = OUTPUT_DIR / "qa_rule_sample_cases.json"

RANDOM_SEED = 20260526

COUNTRIES = [
    {
        "label": "沙特",
        "id_prefix": "saudi",
        "files": [
            ("价值贬损", "devaluation", QA_DIR / "Saudi_QA_价值贬损.json"),
            ("违规协助", "violation_assistance", QA_DIR / "Saudi_QA_违规协助.json"),
        ],
    },
    {
        "label": "泰国",
        "id_prefix": "thailand",
        "files": [
            ("价值贬损", "devaluation", QA_DIR / "Thailand_QA_价值贬损.json"),
            ("违规协助", "violation_assistance", QA_DIR / "Thailand_QA_违规协助.json"),
        ],
    },
    {
        "label": "土耳其",
        "id_prefix": "turkey",
        "files": [
            ("价值贬损", "devaluation", QA_DIR / "Turkey_QA_价值贬损.json"),
            ("违规协助", "violation_assistance", QA_DIR / "Turkey_QA_违规协助.json"),
        ],
    },
]

CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")


def main() -> int:
    rng = random.Random(RANDOM_SEED)
    output: list[dict[str, Any]] = []
    summary: dict[str, dict[str, int]] = {}

    for country in COUNTRIES:
        country_records = [load_json(path) for _, _, path in country["files"]]
        rule_zh_lookup = build_rule_zh_lookup(country_records)
        country_summary: dict[str, int] = {}

        for type_zh, type_id, path in country["files"]:
            records = load_json(path)
            grouped = group_by_rule(records)
            for rule_id in sorted(grouped, key=rule_sort_key):
                selected = rng.choice(grouped[rule_id])
                composite_id = f"{country['id_prefix']}_{type_id}_{rule_id}"
                output.append(
                    {
                        "id": len(output) + 1,
                        "rule_id": composite_id,
                        "rule_zh": rule_zh_lookup.get(rule_id, clean_text(selected.get("rule_zh"))),
                        "case": clean_text(selected.get("case")),
                    }
                )
            country_summary[type_zh] = len(grouped)

        summary[country["label"]] = country_summary

    validate_output(output)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "output": str(OUTPUT_FILE),
                "summary": summary,
                "total": len(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def load_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    records = [item for item in data if isinstance(item, dict)]
    if len(records) != len(data):
        raise ValueError(f"{path} contains non-object records")
    return records


def build_rule_zh_lookup(record_groups: list[list[dict[str, Any]]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    fallback: dict[str, str] = {}
    for records in record_groups:
        for item in records:
            rule_id = clean_text(item.get("rule_id"))
            rule_zh = clean_text(item.get("rule_zh"))
            if not rule_id or not rule_zh:
                continue
            fallback.setdefault(rule_id, rule_zh)
            if CHINESE_RE.search(rule_zh):
                lookup.setdefault(rule_id, rule_zh)
    return {**fallback, **lookup}


def group_by_rule(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        rule_id = clean_text(item.get("rule_id"))
        case = clean_text(item.get("case"))
        if not rule_id or not case:
            continue
        grouped[rule_id].append(item)
    if not grouped:
        raise ValueError("No rule groups found")
    return dict(grouped)


def validate_output(records: list[dict[str, Any]]) -> None:
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate ids found")

    invalid = [
        record["id"]
        for record in records
        if set(record) != {"id", "rule_id", "rule_zh", "case"}
        or not record["rule_id"]
        or not record["rule_zh"]
        or not record["case"]
    ]
    if invalid:
        raise ValueError(f"Invalid records: {invalid[:10]}")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def rule_sort_key(rule_id: str) -> tuple[str, int, str]:
    match = re.match(r"^([A-Za-z]+)(\d+)$", rule_id)
    if not match:
        return (rule_id, -1, rule_id)
    return (match.group(1), int(match.group(2)), rule_id)


if __name__ == "__main__":
    raise SystemExit(main())
