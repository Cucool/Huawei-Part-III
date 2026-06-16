#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
QA_DIR = BASE_DIR / "QA"
OUTPUT_DIR = BASE_DIR / "generated_low_resource_cases"
SUMMARY_FILE = OUTPUT_DIR / "safety_dialect_generalization_summary.json"

COUNTRIES = [
    {
        "language": "arabic",
        "country": "沙特",
        "prefix": "Saudi",
        "rules_file": BASE_DIR / "方言-沙特.py",
        "rules_var": "ARABIC_RULES",
        "script_re": re.compile(r"[\u0600-\u06FF]"),
    },
    {
        "language": "thai",
        "country": "泰国",
        "prefix": "Thailand",
        "rules_file": BASE_DIR / "方言.py",
        "rules_var": "THAILAND_RULES",
        "script_re": re.compile(r"[\u0E00-\u0E7F]"),
    },
    {
        "language": "turkish",
        "country": "土耳其",
        "prefix": "Turkey",
        "rules_file": BASE_DIR / "方言.py",
        "rules_var": "TURKISH_RULES",
        "script_re": re.compile(r"[A-Za-zçğıİöşüÇĞİÖŞÜ]"),
    },
]

QUESTION_TYPES = [
    {"label": "知识问答", "key": "knowledge", "file_kind": "MCQ"},
    {"label": "价值贬损", "key": "value_derogation", "file_kind": "QA"},
    {"label": "违规协助", "key": "violation_assistance", "file_kind": "QA"},
]

TARGET_RULE_SLOTS = 10


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {}

    for spec in COUNTRIES:
        rules = load_rules(spec["rules_file"], spec["rules_var"])
        records_by_type = {
            question_type["key"]: load_question_records(spec["prefix"], question_type)
            for question_type in QUESTION_TYPES
        }
        selected_rules_by_type, rule_stats = select_rule_slots_by_type(
            rules,
            records_by_type,
            spec["script_re"],
        )
        records = build_records(spec, records_by_type, selected_rules_by_type)
        output_file = OUTPUT_DIR / f"{spec['language']}_safety_dialect_generalization.json"
        write_json(output_file, records)

        summary[spec["language"]] = {
            "country": spec["country"],
            "output_file": str(output_file),
            "total": len(records),
            "success": sum(1 for item in records if item["status"] == "success"),
            "failure": sum(1 for item in records if item["status"] != "success"),
            "target_rule_slots_per_type": TARGET_RULE_SLOTS,
            "selected_rule_ids_by_type": {
                type_key: [rule["id"] for rule in selected_rules]
                for type_key, selected_rules in selected_rules_by_type.items()
            },
            "distinct_selected_rule_count_by_type": {
                type_key: len({rule["id"] for rule in selected_rules})
                for type_key, selected_rules in selected_rules_by_type.items()
            },
            "selection_policy": (
                "Select 10 rule slots independently for each QA question type. A rule only needs "
                "to change one case in the current question type; the 3 question types do not share "
                "a simultaneous-match constraint. If fewer than 10 distinct rules match a type, "
                "repeat matching rules from the same type to keep 10 successful cases."
            ),
            "rule_match_stats": rule_stats,
        }

    write_json(SUMMARY_FILE, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def load_rules(path: Path, var_name: str) -> list[dict[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == var_name:
                value = ast.literal_eval(node.value)
                if not isinstance(value, list):
                    raise ValueError(f"{var_name} in {path} must be a list")
                return value
    raise ValueError(f"Could not find {var_name} in {path}")


def load_question_records(prefix: str, question_type: dict[str, str]) -> list[dict[str, str]]:
    if question_type["file_kind"] == "MCQ":
        path = QA_DIR / f"{prefix}_MCQ_{question_type['label']}.json"
        return load_mcq_records(path, question_type)

    path = QA_DIR / f"{prefix}_QA_{question_type['label']}.json"
    return load_qa_records(path, question_type)


def load_mcq_records(path: Path, question_type: dict[str, str]) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    records = []
    for regulation_index, regulation in enumerate(data.get("regulations", []), start=1):
        for item_index, item in enumerate(regulation.get("items", []), start=1):
            category = normalize_text(item.get("category") or regulation.get("category") or "")
            qas = item.get("qa", {}).get("qas", [])
            for qa_index, qa in enumerate(qas, start=1):
                if qa.get("type") != "mcq" or not isinstance(qa.get("question"), str):
                    continue
                records.append(
                    {
                        "source_id": f"reg{regulation_index}_item{item_index}_qa{qa_index}",
                        "task_type": question_type["label"],
                        "task_type_key": question_type["key"],
                        "category": category,
                        "original": format_mcq_case(qa),
                    }
                )
    if not records:
        raise ValueError(f"No MCQ records found in {path}")
    return records


def load_qa_records(path: Path, question_type: dict[str, str]) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")

    records = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict) or not normalize_text(item.get("case")):
            continue
        rule_id = normalize_text(item.get("rule_id")) or f"idx{index}"
        item_index = normalize_text(item.get("idx")) or str(index)
        records.append(
            {
                "source_id": f"{rule_id}_idx{item_index}",
                "source_rule_id": rule_id,
                "task_type": question_type["label"],
                "task_type_key": question_type["key"],
                "category": normalize_text(item.get("category")),
                "original": normalize_text(item["case"]),
            }
        )
    if not records:
        raise ValueError(f"No QA records found in {path}")
    return records


def format_mcq_case(qa: dict[str, Any]) -> str:
    question = normalize_text(qa["question"])
    options = qa.get("options") or {}
    if not isinstance(options, dict) or not options:
        return question
    option_lines = [f"{label}. {normalize_text(value)}" for label, value in sorted(options.items())]
    return "\n".join([question, *option_lines])


def select_rule_slots_by_type(
    rules: list[dict[str, str]],
    records_by_type: dict[str, list[dict[str, str]]],
    script_re: re.Pattern[str],
) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, Any]]]:
    stats = []
    for rule in rules:
        matches_by_type = {
            type_key: count_rule_matches(records, rule, script_re)
            for type_key, records in records_by_type.items()
        }
        total_matches = sum(matches_by_type.values())
        stats.append(
            {
                "rule_id": rule["id"],
                "rule_description": rule["description"],
                "total_matches": total_matches,
                "matches_by_type": matches_by_type,
            }
        )

    selected_by_type = {}
    for type_key in records_by_type:
        eligible_rule_ids = {
            item["rule_id"]
            for item in stats
            if item["matches_by_type"][type_key] > 0
        }
        eligible_rules = [rule for rule in rules if rule["id"] in eligible_rule_ids]
        if not eligible_rules:
            raise ValueError(f"No dialect rule can change question type {type_key}")

        selected = list(eligible_rules[:TARGET_RULE_SLOTS])
        cursor = 0
        while len(selected) < TARGET_RULE_SLOTS:
            selected.append(eligible_rules[cursor % len(eligible_rules)])
            cursor += 1
        selected_by_type[type_key] = selected

    return selected_by_type, stats


def count_rule_matches(
    records: list[dict[str, str]],
    rule: dict[str, str],
    script_re: re.Pattern[str],
) -> int:
    count = 0
    for record in records:
        original = record["original"]
        if script_re.search(original) and apply_rule(original, rule) != original:
            count += 1
    return count


def build_records(
    spec: dict[str, Any],
    records_by_type: dict[str, list[dict[str, str]]],
    selected_rules_by_type: dict[str, list[dict[str, str]]],
) -> list[dict[str, Any]]:
    output = []
    used_sources_by_type: dict[str, set[str]] = defaultdict(set)

    for question_type in QUESTION_TYPES:
        type_key = question_type["key"]
        for slot_index, rule in enumerate(selected_rules_by_type[type_key], start=1):
            match = find_unused_case_for_rule(
                records_by_type[type_key],
                rule,
                used_sources_by_type[type_key],
            )
            if match is None:
                raise ValueError(
                    f"{spec['language']} {question_type['label']} has no unused case for rule {rule['id']}"
                )

            used_sources_by_type[type_key].add(match["source_id"])
            output.append(
                {
                    "id": len(output) + 1,
                    "language": spec["language"],
                    "country": spec["country"],
                    "group": "content_safety",
                    "task_type": question_type["label"],
                    "task_type_key": type_key,
                    "rule_slot": slot_index,
                    "rule_id": rule["id"],
                    "rule_description": rule["description"],
                    "dialect_rule": f"{rule['id']}: {rule['description']}",
                    "source_id": match["source_id"],
                    "category": match["category"],
                    "original": match["original"],
                    "generated": match["generated"],
                    "status": "success",
                    "error": "",
                }
            )

    return output


def find_unused_case_for_rule(
    records: list[dict[str, str]],
    rule: dict[str, str],
    used_source_ids: set[str],
) -> dict[str, str] | None:
    fallback = None
    for record in records:
        generated = apply_rule(record["original"], rule)
        if generated == record["original"]:
            continue
        match = {**record, "generated": generated}
        if record["source_id"] not in used_source_ids:
            return match
        if fallback is None:
            fallback = match
    return fallback


def apply_rule(text: str, rule: dict[str, str]) -> str:
    return re.sub(rule["pattern"], rule["replace"], text)


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def rule_sort_key(rule_id: str) -> tuple[int, int, str]:
    numbers = [int(part) for part in re.findall(r"\d+", rule_id)]
    primary = numbers[0] if numbers else 10**9
    secondary = numbers[1] if len(numbers) > 1 else 0
    return primary, secondary, rule_id


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
