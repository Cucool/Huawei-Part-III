#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
QA_DIR = BASE_DIR / "QA"
LOW_RESOURCE_DIR = BASE_DIR / "generated_low_resource_cases"
QA_REWRITE_DIR = BASE_DIR / "generated_qa_rewrites"
QA_REWRITE_FILE = QA_REWRITE_DIR / "qa_local_model_rewrites.json"
QA_RULE_DIR = BASE_DIR / "generated_qa_rule_cases"
QA_RULE_FILE = QA_RULE_DIR / "qa_rule_sample_cases.json"
JAILBREAK_PRO_DATA_DIR = BASE_DIR / "jailbreak" / "pro_data"
SUMMARY_FILE = LOW_RESOURCE_DIR / "content_safety_nonoverlap_summary.json"
SAFETY_DIALECT_SUMMARY_FILE = LOW_RESOURCE_DIR / "safety_dialect_generalization_summary.json"

RANDOM_SEED = 20260612
TARGET_DIALECT_RULE_SLOTS = 10

COUNTRIES = [
    {
        "country": "沙特",
        "country_key": "saudi",
        "language": "arabic",
        "prefix": "Saudi",
        "rules_file": BASE_DIR / "方言-沙特.py",
        "rules_var": "ARABIC_RULES",
        "script_re": re.compile(r"[\u0600-\u06FF]"),
        "jailbreak_output": "QA_arabic.json",
    },
    {
        "country": "泰国",
        "country_key": "thailand",
        "language": "thai",
        "prefix": "Thailand",
        "rules_file": BASE_DIR / "方言.py",
        "rules_var": "THAILAND_RULES",
        "script_re": re.compile(r"[\u0E00-\u0E7F]"),
        "jailbreak_output": "QA_thai.json",
    },
    {
        "country": "土耳其",
        "country_key": "turkey",
        "language": "turkish",
        "prefix": "Turkey",
        "rules_file": BASE_DIR / "方言.py",
        "rules_var": "TURKISH_RULES",
        "script_re": re.compile(r"[A-Za-zçğıİöşüÇĞİÖŞÜ]"),
        "jailbreak_output": "QA_turkish.json",
    },
]

QUESTION_TYPES = [
    {"label": "知识问答", "key": "knowledge", "kind": "mcq", "file_stem": "MCQ_知识问答", "rule_sample_key": None},
    {"label": "价值贬损", "key": "value_derogation", "kind": "qa", "file_stem": "QA_价值贬损", "rule_sample_key": "devaluation"},
    {"label": "违规协助", "key": "violation_assistance", "kind": "qa", "file_stem": "QA_违规协助", "rule_sample_key": "violation_assistance"},
]

JAILBREAK_QUESTION_TYPE_KEYS = ("value_derogation", "violation_assistance")


def main() -> int:
    rng = random.Random(RANDOM_SEED)
    LOW_RESOURCE_DIR.mkdir(parents=True, exist_ok=True)
    QA_REWRITE_DIR.mkdir(parents=True, exist_ok=True)
    QA_RULE_DIR.mkdir(parents=True, exist_ok=True)
    JAILBREAK_PRO_DATA_DIR.mkdir(parents=True, exist_ok=True)

    sources_by_language = {spec["language"]: load_country_sources(spec) for spec in COUNTRIES}
    qwen_records, qwen_used_by_language = enrich_qwen_rewrites(sources_by_language)

    dialect_outputs: dict[str, list[dict[str, Any]]] = {}
    jailbreak_outputs: dict[str, list[dict[str, Any]]] = {}
    qa_rule_records: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "seed": RANDOM_SEED,
        "source_key_definition": "language + source_file + source_idx",
        "policy": (
            "For each language, source cases are sampled without replacement across content-safety "
            "dialect generalization, local-model rewrite, and jailbreak seed inputs. Existing local "
            "model rewrite text is preserved, so those source_key values are reserved before "
            "regenerating dialect and jailbreak seed files."
        ),
        "outputs": {},
        "languages": {},
    }
    safety_dialect_summary: dict[str, Any] = {}

    for spec in COUNTRIES:
        language = spec["language"]
        used_source_keys = set(qwen_used_by_language[language])
        rules = load_rules(spec["rules_file"], spec["rules_var"])
        sources_by_type = group_sources_by_type(sources_by_language[language])

        dialect_records, dialect_rule_stats = build_safety_dialect_records(
            spec=spec,
            rules=rules,
            sources_by_type=sources_by_type,
            used_source_keys=used_source_keys,
            rng=rng,
        )
        dialect_outputs[language] = dialect_records
        write_json(LOW_RESOURCE_DIR / f"{language}_safety_dialect_generalization.json", dialect_records)

        jailbreak_records, qa_rule_language_records = build_jailbreak_seed_records(
            spec=spec,
            sources_by_type=sources_by_type,
            used_source_keys=used_source_keys,
            start_rule_case_id=len(qa_rule_records) + 1,
            rng=rng,
        )
        jailbreak_outputs[language] = jailbreak_records
        qa_rule_records.extend(qa_rule_language_records)
        write_json(JAILBREAK_PRO_DATA_DIR / spec["jailbreak_output"], jailbreak_records)

        language_groups = {
            "safety_dialect": dialect_records,
            "qwen_rewrite": [item for item in qwen_records if item.get("language") == language],
            "jailbreak_seed": jailbreak_records,
        }
        duplicate_source_keys = duplicate_keys(
            item["source_key"]
            for group in language_groups.values()
            for item in group
            if item.get("source_key")
        )
        if duplicate_source_keys:
            raise ValueError(f"{language} still has duplicated source_key values: {duplicate_source_keys[:10]}")

        summary["languages"][language] = {
            "country": spec["country"],
            "counts": {name: len(items) for name, items in language_groups.items()},
            "duplicate_source_keys": duplicate_source_keys,
            "qwen_reserved_source_keys": sorted(qwen_used_by_language[language]),
            "dialect_source_keys": sorted(item["source_key"] for item in dialect_records),
            "jailbreak_seed_source_keys": sorted(item["source_key"] for item in jailbreak_records),
        }
        safety_dialect_summary[language] = {
            "country": spec["country"],
            "output_file": str(LOW_RESOURCE_DIR / f"{language}_safety_dialect_generalization.json"),
            "total": len(dialect_records),
            "success": sum(1 for item in dialect_records if item["status"] == "success"),
            "failure": sum(1 for item in dialect_records if item["status"] != "success"),
            "target_rule_slots_per_type": TARGET_DIALECT_RULE_SLOTS,
            "selection_policy": (
                "Select 10 dialect rule slots for each QA type. For each language, source cases "
                "are selected without replacement and cannot reuse source_key values reserved by "
                "local-model rewrite or already selected by earlier dialect slots."
            ),
            "rule_match_stats": dialect_rule_stats,
        }

    write_json(QA_REWRITE_FILE, qwen_records)
    write_json(QA_RULE_FILE, qa_rule_records)
    write_json(
        QA_REWRITE_DIR / "summary.json",
        {
            "output": str(QA_REWRITE_FILE),
            "model": infer_qwen_model(qwen_records),
            "total": len(qwen_records),
            "success": sum(1 for item in qwen_records if item.get("status") == "success"),
            "failure": sum(1 for item in qwen_records if item.get("status") != "success"),
            "note": "Existing rewrite text was preserved; this run added source_file/source_idx/source_key metadata.",
        },
    )
    summary["outputs"] = {
        "safety_dialect_summary": str(SAFETY_DIALECT_SUMMARY_FILE),
        "qa_rewrites": str(QA_REWRITE_FILE),
        "qa_rule_sample_cases": str(QA_RULE_FILE),
        "jailbreak_pro_data_dir": str(JAILBREAK_PRO_DATA_DIR),
    }
    write_json(SAFETY_DIALECT_SUMMARY_FILE, safety_dialect_summary)
    write_json(SUMMARY_FILE, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def load_country_sources(spec: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for question_type in QUESTION_TYPES:
        if question_type["kind"] == "mcq":
            path = QA_DIR / f"{spec['prefix']}_{question_type['file_stem']}.json"
            records.extend(load_mcq_sources(path, spec, question_type))
        else:
            path = QA_DIR / f"{spec['prefix']}_{question_type['file_stem']}.json"
            records.extend(load_qa_sources(path, spec, question_type))
    return records


def load_mcq_sources(path: Path, spec: dict[str, Any], question_type: dict[str, Any]) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    output = []
    for regulation_index, regulation in enumerate(data.get("regulations", []), start=1):
        for item_index, item in enumerate(regulation.get("items", []), start=1):
            category = normalize_text(item.get("category") or regulation.get("category") or "")
            for qa_index, qa in enumerate(item.get("qa", {}).get("qas", []), start=1):
                if qa.get("type") != "mcq" or not isinstance(qa.get("question"), str):
                    continue
                source_idx = f"reg{regulation_index}_item{item_index}_qa{qa_index}"
                source_file = str(path.relative_to(BASE_DIR))
                source_id = source_idx
                output.append(
                    make_source_record(
                        spec=spec,
                        question_type=question_type,
                        source_file=source_file,
                        source_idx=source_idx,
                        source_id=source_id,
                        category=category,
                        case=format_mcq_case(qa),
                        source_rule_id="",
                        source_rule_zh="",
                        if_review="",
                    )
                )
    if not output:
        raise ValueError(f"No MCQ sources found in {path}")
    return output


def load_qa_sources(path: Path, spec: dict[str, Any], question_type: dict[str, Any]) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    output = []
    for row_index, item in enumerate(data, start=1):
        if not isinstance(item, dict) or not normalize_text(item.get("case")):
            continue
        source_idx = normalize_text(item.get("idx")) or str(row_index)
        source_file = str(path.relative_to(BASE_DIR))
        source_rule_id = normalize_text(item.get("rule_id"))
        source_id = f"{source_rule_id}_idx{source_idx}" if source_rule_id else f"idx{source_idx}"
        output.append(
            make_source_record(
                spec=spec,
                question_type=question_type,
                source_file=source_file,
                source_idx=source_idx,
                source_id=source_id,
                category=normalize_text(item.get("category")),
                case=normalize_text(item.get("case")),
                source_rule_id=source_rule_id,
                source_rule_zh=normalize_text(item.get("rule_zh")),
                if_review=normalize_text(item.get("if_review")),
            )
        )
    if not output:
        raise ValueError(f"No QA sources found in {path}")
    return output


def make_source_record(
    *,
    spec: dict[str, Any],
    question_type: dict[str, Any],
    source_file: str,
    source_idx: str,
    source_id: str,
    category: str,
    case: str,
    source_rule_id: str,
    source_rule_zh: str,
    if_review: str,
) -> dict[str, Any]:
    source_key = make_source_key(spec["language"], source_file, source_idx)
    prefixed_source_id = f"{spec['country_key']}_{question_type['key']}_{source_id}"
    return {
        "language": spec["language"],
        "country": spec["country"],
        "country_key": spec["country_key"],
        "task_type": question_type["label"],
        "task_type_key": question_type["key"],
        "source_file": source_file,
        "source_idx": source_idx,
        "source_id": source_id,
        "prefixed_source_id": prefixed_source_id,
        "source_key": source_key,
        "source_rule_id": source_rule_id,
        "source_rule_zh": source_rule_zh,
        "category": category,
        "case": case,
        "if_review": if_review,
    }


def enrich_qwen_rewrites(
    sources_by_language: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    raw_records = load_json_list(QA_REWRITE_FILE)
    source_indexes = {
        language: build_source_indexes(sources)
        for language, sources in sources_by_language.items()
    }
    language_by_country = {spec["country"]: spec["language"] for spec in COUNTRIES}
    used_by_language: dict[str, set[str]] = defaultdict(set)
    output = []

    for item in raw_records:
        language = item.get("language") or language_by_country.get(normalize_text(item.get("country")), "")
        if not language:
            raise ValueError(f"Cannot infer language for qwen rewrite record: {item}")
        source = find_source_for_qwen_item(item, source_indexes[language])
        record = dict(item)
        record["language"] = language
        attach_source_metadata(record, source)
        used_by_language[language].add(source["source_key"])
        output.append(record)

    return output, used_by_language


def find_source_for_qwen_item(item: dict[str, Any], indexes: dict[str, Any]) -> dict[str, Any]:
    original = normalize_text(item.get("original"))
    source_id = normalize_text(item.get("source_id"))
    if original and original in indexes["by_case"]:
        return indexes["by_case"][original]
    if source_id and source_id in indexes["by_prefixed_source_id"]:
        return indexes["by_prefixed_source_id"][source_id]

    # Backward compatibility with source ids like thailand_value_derogation_R001.
    parts = source_id.split("_")
    if len(parts) >= 3:
        rule_id = parts[-1]
        type_key = "_".join(parts[1:-1])
        candidates = indexes["by_type_and_rule"].get((type_key, rule_id), [])
        if candidates:
            return candidates[0]

    raise ValueError(f"Cannot map qwen rewrite record to QA source: {item}")


def build_source_indexes(sources: list[dict[str, Any]]) -> dict[str, Any]:
    by_case = {}
    by_prefixed_source_id = {}
    by_type_and_rule: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for source in sources:
        by_case.setdefault(normalize_text(source["case"]), source)
        by_prefixed_source_id[source["prefixed_source_id"]] = source
        if source.get("source_rule_id"):
            by_type_and_rule[(source["task_type_key"], source["source_rule_id"])].append(source)
    return {
        "by_case": by_case,
        "by_prefixed_source_id": by_prefixed_source_id,
        "by_type_and_rule": dict(by_type_and_rule),
    }


def build_safety_dialect_records(
    *,
    spec: dict[str, Any],
    rules: list[dict[str, str]],
    sources_by_type: dict[str, list[dict[str, Any]]],
    used_source_keys: set[str],
    rng: random.Random,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    output = []
    stats = build_rule_match_stats(rules, sources_by_type, spec["script_re"], used_source_keys)
    selected_rules_by_type = select_dialect_rules(rules, stats)

    for question_type in QUESTION_TYPES:
        type_key = question_type["key"]
        for slot_index, rule in enumerate(selected_rules_by_type[type_key], start=1):
            candidates = [
                {**source, "generated": apply_rule(source["case"], rule)}
                for source in sources_by_type[type_key]
                if source["source_key"] not in used_source_keys
                and spec["script_re"].search(source["case"])
                and apply_rule(source["case"], rule) != source["case"]
            ]
            if not candidates:
                raise ValueError(f"{spec['language']} {question_type['label']} has no unused source for rule {rule['id']}")
            match = rng.choice(candidates)
            used_source_keys.add(match["source_key"])
            record = {
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
                "source_file": match["source_file"],
                "source_idx": match["source_idx"],
                "source_key": match["source_key"],
                "source_rule_id": match.get("source_rule_id", ""),
                "source_rule_zh": match.get("source_rule_zh", ""),
                "category": match["category"],
                "original": match["case"],
                "generated": match["generated"],
                "status": "success",
                "error": "",
            }
            output.append(record)

    return output, stats


def build_rule_match_stats(
    rules: list[dict[str, str]],
    sources_by_type: dict[str, list[dict[str, Any]]],
    script_re: re.Pattern[str],
    reserved_source_keys: set[str],
) -> list[dict[str, Any]]:
    stats = []
    for rule in rules:
        matches_by_type = {}
        unused_matches_by_type = {}
        for question_type in QUESTION_TYPES:
            type_key = question_type["key"]
            sources = sources_by_type[type_key]
            matches = [
                source
                for source in sources
                if script_re.search(source["case"]) and apply_rule(source["case"], rule) != source["case"]
            ]
            matches_by_type[type_key] = len(matches)
            unused_matches_by_type[type_key] = sum(1 for source in matches if source["source_key"] not in reserved_source_keys)
        stats.append(
            {
                "rule_id": rule["id"],
                "rule_description": rule["description"],
                "total_matches": sum(matches_by_type.values()),
                "matches_by_type": matches_by_type,
                "unused_matches_by_type_after_qwen_reservation": unused_matches_by_type,
            }
        )
    return stats


def select_dialect_rules(rules: list[dict[str, str]], stats: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    stats_by_rule = {item["rule_id"]: item for item in stats}
    selected_by_type = {}
    for question_type in QUESTION_TYPES:
        type_key = question_type["key"]
        eligible_rules = [
            rule
            for rule in rules
            if stats_by_rule[rule["id"]]["unused_matches_by_type_after_qwen_reservation"][type_key] > 0
        ]
        if not eligible_rules:
            raise ValueError(f"No dialect rule can change question type {type_key}")
        selected = list(eligible_rules[:TARGET_DIALECT_RULE_SLOTS])
        cursor = 0
        while len(selected) < TARGET_DIALECT_RULE_SLOTS:
            selected.append(eligible_rules[cursor % len(eligible_rules)])
            cursor += 1
        selected_by_type[type_key] = selected
    return selected_by_type


def build_jailbreak_seed_records(
    *,
    spec: dict[str, Any],
    sources_by_type: dict[str, list[dict[str, Any]]],
    used_source_keys: set[str],
    start_rule_case_id: int,
    rng: random.Random,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pro_data = []
    qa_rule_records = []
    next_id = start_rule_case_id

    for type_key in JAILBREAK_QUESTION_TYPE_KEYS:
        question_type = next(item for item in QUESTION_TYPES if item["key"] == type_key)
        grouped = group_by_rule(sources_by_type[type_key])
        for raw_rule_id in sorted(grouped, key=rule_sort_key):
            candidates = [item for item in grouped[raw_rule_id] if item["source_key"] not in used_source_keys]
            if not candidates:
                raise ValueError(f"{spec['language']} {question_type['label']} {raw_rule_id} has no unused source")
            reviewed = [item for item in candidates if normalize_text(item.get("if_review")).lower() == "yes"]
            selected = rng.choice(reviewed or candidates)
            used_source_keys.add(selected["source_key"])

            composite_rule_id = f"{spec['country_key']}_{question_type['rule_sample_key']}_{raw_rule_id}"
            qa_rule_record = {
                "id": next_id,
                "country": spec["country"],
                "type": question_type["label"],
                "rule_id": composite_rule_id,
                "rule_zh": selected.get("source_rule_zh", ""),
                "case": selected["case"],
                "source_file": selected["source_file"],
                "source_idx": selected["source_idx"],
                "source_id": selected["source_id"],
                "source_key": selected["source_key"],
                "source_rule_id": raw_rule_id,
                "category": selected.get("category", ""),
            }
            pro_record = {
                "id": next_id,
                "category": "QA",
                "language": spec["language"],
                "country": spec["country"],
                "task_type": question_type["label"],
                "task_type_key": type_key,
                "case": selected["case"],
                "source_file": selected["source_file"],
                "source_idx": selected["source_idx"],
                "source_id": selected["source_id"],
                "source_key": selected["source_key"],
                "source_item": None,
                "source_rule_id": composite_rule_id,
                "source_rule_zh": selected.get("source_rule_zh", ""),
                "category_label": selected.get("category", ""),
            }
            qa_rule_records.append(qa_rule_record)
            pro_data.append(pro_record)
            next_id += 1

    return pro_data, qa_rule_records


def group_sources_by_type(sources: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped = {item["key"]: [] for item in QUESTION_TYPES}
    for source in sources:
        grouped[source["task_type_key"]].append(source)
    return grouped


def group_by_rule(sources: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in sources:
        rule_id = normalize_text(source.get("source_rule_id"))
        if rule_id:
            grouped[rule_id].append(source)
    if not grouped:
        raise ValueError("No rule groups found")
    return dict(grouped)


def attach_source_metadata(record: dict[str, Any], source: dict[str, Any]) -> None:
    record["source_file"] = source["source_file"]
    record["source_idx"] = source["source_idx"]
    record["source_key"] = source["source_key"]
    record["source_rule_id"] = source.get("source_rule_id", "")
    record["source_rule_zh"] = source.get("source_rule_zh", "")
    record["category"] = source.get("category", "")


def format_mcq_case(qa: dict[str, Any]) -> str:
    question = normalize_text(qa["question"])
    options = qa.get("options") or {}
    if not isinstance(options, dict) or not options:
        return question
    option_lines = [f"{label}. {normalize_text(value)}" for label, value in sorted(options.items())]
    return "\n".join([question, *option_lines])


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


def apply_rule(text: str, rule: dict[str, str]) -> str:
    return re.sub(rule["pattern"], rule["replace"], text)


def make_source_key(language: str, source_file: str, source_idx: str) -> str:
    return f"{language}|{source_file}|{source_idx}"


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def rule_sort_key(rule_id: str) -> tuple[str, int, str]:
    match = re.match(r"^([A-Za-z]+)(\d+)$", normalize_text(rule_id))
    if not match:
        return (normalize_text(rule_id), -1, normalize_text(rule_id))
    return (match.group(1), int(match.group(2)), normalize_text(rule_id))


def duplicate_keys(values: Any) -> list[str]:
    seen = set()
    duplicates = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def infer_qwen_model(records: list[dict[str, Any]]) -> str:
    models = [normalize_text(item.get("model")) for item in records if normalize_text(item.get("model"))]
    return models[0] if models else ""


def load_json_list(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return [item for item in data if isinstance(item, dict)]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
