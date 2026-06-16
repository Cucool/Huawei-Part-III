#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
QA_DIR = BASE_DIR / "QA"
OUTPUT_DIR = BASE_DIR / "generated_qa_eval_cases"
COMBINED_OUTPUT = OUTPUT_DIR / "qa_eval_cases.json"

COUNTRIES = [
    {
        "country_key": "saudi",
        "file_prefix": "Saudi",
        "expected_rules": 57,
    },
    {
        "country_key": "thailand",
        "file_prefix": "Thailand",
        "expected_rules": 52,
    },
    {
        "country_key": "turkey",
        "file_prefix": "Turkey",
        "expected_rules": 50,
    },
]

QUESTION_TYPES = [
    {
        "type_key": "violation_assist",
        "file_suffix": "违规协助",
    },
    {
        "type_key": "value_derogation",
        "file_suffix": "价值贬损",
    },
]


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    combined_records: list[dict[str, str]] = []
    summary: dict[str, Any] = {}

    for country in COUNTRIES:
        country_records: list[dict[str, str]] = []
        country_summary: dict[str, Any] = {
            "expected_rules_per_type": country["expected_rules"],
            "types": {},
        }

        for question_type in QUESTION_TYPES:
            path = QA_DIR / f"{country['file_prefix']}_QA_{question_type['file_suffix']}.json"
            records = load_records(path)
            selected = select_one_per_rule(records)
            target_rule_ids = expected_rule_ids(country["expected_rules"])
            source_rule_ids = set(selected)
            missing_rule_ids = [rule_id for rule_id in target_rule_ids if rule_id not in source_rule_ids]
            extra_rule_ids = sorted(source_rule_ids - set(target_rule_ids), key=rule_sort_key)
            selected_rule_ids = [rule_id for rule_id in target_rule_ids if rule_id in selected]

            type_records = [
                make_output_record(
                    country_key=country["country_key"],
                    type_key=question_type["type_key"],
                    item=selected[rule_id],
                )
                for rule_id in selected_rule_ids
            ]
            country_records.extend(type_records)

            country_summary["types"][question_type["file_suffix"]] = {
                "source_file": str(path),
                "source_records": len(records),
                "source_unique_rules": len(source_rule_ids),
                "expected_rules": country["expected_rules"],
                "selected_records": len(type_records),
                "missing_expected_rules": missing_rule_ids,
                "ignored_extra_rules": extra_rule_ids,
                "rule_count_matches_expected": len(type_records) == country["expected_rules"],
            }

        country_output = OUTPUT_DIR / f"{country['country_key']}_qa_eval_cases.json"
        write_json(country_output, country_records)
        combined_records.extend(country_records)

        country_summary["output_file"] = str(country_output)
        country_summary["total_selected"] = len(country_records)
        country_summary["expected_total"] = country["expected_rules"] * len(QUESTION_TYPES)
        country_summary["total_matches_expected"] = country_summary["total_selected"] == country_summary["expected_total"]
        summary[country["country_key"]] = country_summary

    validate_output(combined_records)
    write_json(COMBINED_OUTPUT, combined_records)
    write_json(OUTPUT_DIR / "summary.json", summary)

    print(json.dumps({"output": str(COMBINED_OUTPUT), "summary": summary, "total": len(combined_records)}, ensure_ascii=False, indent=2))
    return 0


def load_records(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")

    records = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{path} item {index} is not an object")
        for key in ("rule_id", "rule_zh", "case"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"{path} item {index} missing non-empty {key}")
        records.append(item)
    return records


def select_one_per_rule(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        grouped[item["rule_id"].strip()].append(item)

    selected = {}
    for rule_id, items in grouped.items():
        reviewed = [item for item in items if str(item.get("if_review", "")).strip().lower() == "yes"]
        selected[rule_id] = reviewed[0] if reviewed else items[0]
    return selected


def expected_rule_ids(count: int) -> list[str]:
    return [f"R{index:03d}" for index in range(1, count + 1)]


def rule_sort_key(rule_id: str) -> tuple[int, str]:
    match = re.search(r"\d+", rule_id)
    return (int(match.group(0)) if match else 10**9, rule_id)


def make_output_record(*, country_key: str, type_key: str, item: dict[str, Any]) -> dict[str, str]:
    rule_id = item["rule_id"].strip()
    return {
        "id": f"{country_key}_{type_key}_{rule_id.lower()}",
        "rule_id": rule_id,
        "rule_zh": normalize_text(item["rule_zh"]),
        "case": normalize_text(item["case"]),
    }


def validate_output(records: list[dict[str, str]]) -> None:
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate output ids found")

    for index, record in enumerate(records, start=1):
        if set(record) != {"id", "rule_id", "rule_zh", "case"}:
            raise ValueError(f"Unexpected fields in output record {index}: {sorted(record)}")
        if not all(isinstance(value, str) and value.strip() for value in record.values()):
            raise ValueError(f"Empty value in output record {index}")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
