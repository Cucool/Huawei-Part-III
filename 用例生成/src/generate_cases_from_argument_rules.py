#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import re
from pathlib import Path 
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
CODE_ROOT = BASE_DIR.parent
WUTONG_DATA_DIR = CODE_ROOT / "wutong" / "data"
OUTPUT_DIR = BASE_DIR / "generated_low_resource_cases"

LANGUAGES = {
    "thai": {
        "template_file": BASE_DIR / "th.txt",
        "rules_file": BASE_DIR / "方言.py",
        "rules_var": "THAILAND_RULES",
        "script_re": re.compile(r"[\u0E00-\u0E7F]"),
    },
    "turkish": {
        "template_file": BASE_DIR / "tu.txt",
        "rules_file": BASE_DIR / "方言.py",
        "rules_var": "TURKISH_RULES",
        "script_re": re.compile(r"[çğıİöşüÇĞİÖŞÜ]"),
    },
    "arabic": {
        "template_file": BASE_DIR / "ar.txt",
        "rules_file": BASE_DIR / "方言-沙特.py",
        "rules_var": "ARABIC_RULES",
        "script_re": re.compile(r"[\u0600-\u06FF]"),
    },
}

SAFETY_FILES = {
    "copyright": WUTONG_DATA_DIR / "copyright.json",
    "privacy": WUTONG_DATA_DIR / "privacy.json",
    "QA": WUTONG_DATA_DIR / "QA.json",
}


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {}

    for language, spec in LANGUAGES.items():
        rules = load_rules(spec["rules_file"], spec["rules_var"])
        templates = load_templates(spec["template_file"])

        privacy = build_privacy_examples(language, templates, rules)
        rule_stats = score_rules(spec["script_re"], rules)
        eligible_rule_stats = [item for item in rule_stats if matches_every_safety_type(item)]
        top_rules = [item["rule"] for item in eligible_rule_stats[:5]]
        safety = build_safety_examples(language, spec["script_re"], top_rules)

        write_json(OUTPUT_DIR / f"{language}_privacy_dialect.json", privacy)
        write_json(OUTPUT_DIR / f"{language}_safety_dialect.json", safety)

        summary[language] = {
            "source_rules_file": str(spec["rules_file"]),
            "source_rules_var": spec["rules_var"],
            "privacy_template_count": len(templates),
            "privacy_record_policy": "record every dialect rule that matches each privacy template",
            "privacy_total": len(privacy),
            "privacy_success": count_success(privacy),
            "privacy_failure": count_failure(privacy),
            "top_rule_ids": [rule.get("id") for rule in top_rules],
            "top_rule_policy": "top 5 rules by total matches among rules that match copyright/privacy/QA at least once each",
            "eligible_rule_count": len(eligible_rule_stats),
            "rule_match_stats": [strip_rule_from_stat(item) for item in rule_stats],
            "safety_total": len(safety),
            "safety_success": count_success(safety),
            "safety_failure": count_failure(safety),
            "expected_safety": 15,
        }

    write_json(OUTPUT_DIR / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def load_rules(path: Path, var_name: str) -> list[dict[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    value = ast.literal_eval(node.value)
                    if not isinstance(value, list):
                        raise ValueError(f"{var_name} in {path} must be a list")
                    return value
    raise ValueError(f"Could not find {var_name} in {path}")


def load_templates(path: Path) -> list[str]:
    templates = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(templates) < 10:
        raise ValueError(f"{path} must contain at least 10 templates")
    return templates[:10]


def build_privacy_examples(language: str, templates: list[str], rules: list[dict[str, str]]) -> list[dict[str, Any]]:
    examples = []
    for idx, template in enumerate(templates, start=1):
        matches = apply_all_matching_rules(template, rules)
        if not matches:
            examples.append(make_record(language, "privacy_template", idx, None, template, None))
            continue
        for match in matches:
            examples.append(make_record(language, "privacy_template", idx, None, template, match))
    return examples


def score_rules(script_re: re.Pattern[str], rules: list[dict[str, str]]) -> list[dict[str, Any]]:
    records_by_type = {
        task_type: load_case_records(path)
        for task_type, path in SAFETY_FILES.items()
    }
    stats = []
    for rule in rules:
        by_type = {}
        total = 0
        for task_type, records in records_by_type.items():
            count = count_rule_matches(records, script_re, rule)
            by_type[task_type] = count
            total += count
        stats.append({
            "rule": rule,
            "rule_id": rule.get("id"),
            "rule_description": rule.get("description"),
            "total_matches": total,
            "matches_by_type": by_type,
        })
    return sorted(stats, key=lambda item: (-item["total_matches"], str(item["rule_id"])))


def strip_rule_from_stat(stat: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in stat.items() if key != "rule"}


def matches_every_safety_type(stat: dict[str, Any]) -> bool:
    by_type = stat["matches_by_type"]
    return all(by_type.get(task_type, 0) > 0 for task_type in SAFETY_FILES)


def count_rule_matches(records: list[dict[str, Any]], script_re: re.Pattern[str], rule: dict[str, str]) -> int:
    count = 0
    for item in records:
        original = item["case"].strip()
        if original and script_re.search(original) and apply_rule(original, rule) != original:
            count += 1
    return count


def build_safety_examples(language: str, script_re: re.Pattern[str], rules: list[dict[str, str]]) -> list[dict[str, Any]]:
    examples = []
    for task_type, path in SAFETY_FILES.items():
        records = load_case_records(path)
        for rule in rules[:5]:
            match = find_case_for_rule(records, script_re, rule)
            if match is None:
                examples.append(
                    {
                        "language": language,
                        "group": "content_safety",
                        "task_type": task_type,
                        "rule_id": rule.get("id"),
                        "rule_description": rule.get("description"),
                        "source_id": None,
                        "source_index": None,
                        "original": "",
                        "generated": "",
                        "status": "failure",
                        "error": "No case matched this rule in this task file.",
                    }
                )
            else:
                source_index, source_id, original, generated = match
                examples.append(
                    {
                        "language": language,
                        "group": "content_safety",
                        "task_type": task_type,
                        "rule_id": rule.get("id"),
                        "rule_description": rule.get("description"),
                        "source_id": source_id,
                        "source_index": source_index,
                        "original": original,
                        "generated": generated,
                        "status": "success",
                        "error": "",
                    }
                )
    return examples


def load_case_records(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must be a list")
    return [item for item in data if isinstance(item, dict) and isinstance(item.get("case"), str)]


def find_case_for_rule(
    records: list[dict[str, Any]],
    script_re: re.Pattern[str],
    rule: dict[str, str],
) -> tuple[int, Any, str, str] | None:
    for index, item in enumerate(records):
        original = item["case"].strip()
        if not original or not script_re.search(original):
            continue
        generated = apply_rule(original, rule)
        if generated != original:
            return index, item.get("id"), original, generated
    return None


def apply_all_matching_rules(text: str, rules: list[dict[str, str]]) -> list[dict[str, Any]]:
    matches = []
    for rule in rules:
        generated = apply_rule(text, rule)
        if generated != text:
            matches.append({
                "rule_id": rule.get("id"),
                "rule_description": rule.get("description"),
                "generated": generated,
            })
    return matches


def apply_rule(text: str, rule: dict[str, str]) -> str:
    return re.sub(rule["pattern"], rule["replace"], text)


def make_record(
    language: str,
    group: str,
    index: int,
    task_type: str | None,
    original: str,
    match: dict[str, Any] | None,
) -> dict[str, Any]:
    if match is None:
        return {
            "language": language,
            "group": group,
            "template_index": index,
            "task_type": task_type,
            "rule_id": None,
            "rule_description": None,
            "original": original,
            "generated": "",
            "status": "failure",
            "error": "No dialect rule matched this template.",
        }
    return {
        "language": language,
        "group": group,
        "template_index": index,
        "task_type": task_type,
        "rule_id": match["rule_id"],
        "rule_description": match["rule_description"],
        "original": original,
        "generated": match["generated"],
        "status": "success",
        "error": "",
    }


def count_success(records: list[dict[str, Any]]) -> int:
    return sum(1 for item in records if item.get("status") == "success")


def count_failure(records: list[dict[str, Any]]) -> int:
    return sum(1 for item in records if item.get("status") != "success")


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
