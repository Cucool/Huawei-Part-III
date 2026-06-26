#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
QA_DIR = BASE_DIR / "QA"
EN_DATA_DIR = BASE_DIR / "jailbreak" / "en_data"
LOW_RESOURCE_DIR = BASE_DIR / "用例生成" / "generated_low_resource_cases"
QA_REWRITE_PATH = BASE_DIR / "用例生成" / "generated" / "generated_qa_rewrites" / "qa_local_model_rewrites.json"
OUTPUT_DIR = BASE_DIR / "generated_reinforced_qa_eval_cases"
SUMMARY_PATH = OUTPUT_DIR / "summary.json"


COUNTRIES = [
    {
        "country_dir": "Saudi",
        "country": "沙特",
        "language": "arabic",
        "prefix": "Saudi",
        "en_data_file": "QA_arabic.json",
    },
    {
        "country_dir": "Thailand",
        "country": "泰国",
        "language": "thai",
        "prefix": "Thailand",
        "en_data_file": "QA_thai.json",
    },
    {
        "country_dir": "Turkey",
        "country": "土耳其",
        "language": "turkish",
        "prefix": "Turkey",
        "en_data_file": "QA_turkish.json",
    },
]


QUESTION_TYPES = [
    {
        "label": "知识问答",
        "task_type_key": "knowledge",
        "file_stem": "MCQ_知识问答",
        "structure": "mcq",
    },
    {
        "label": "价值贬损",
        "task_type_key": "value_derogation",
        "file_stem": "QA_价值贬损",
        "structure": "qa_list",
    },
    {
        "label": "违规协助",
        "task_type_key": "violation_assistance",
        "file_stem": "QA_违规协助",
        "structure": "qa_list",
    },
]


def main() -> int:
    removals, en_data_summary = load_en_data_removals()
    expert_removals, expert_review_summary = load_expert_review_removals()
    merge_removal_maps(removals, expert_removals)
    available_source_idxs = collect_available_source_idxs()
    validate_removals(removals, available_source_idxs)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "output_dir": "generated_reinforced_qa_eval_cases",
        "deduplication_policy": (
            "Regenerate reinforced QA evaluation cases from QA source files and remove cases that "
            "already appear in jailbreak/en_data/QA_*.json. QA list files are removed by "
            "source_file + original_case first, so idx renumbering does not remove the wrong row. "
            "source_idx is only used as a fallback when original_case is unavailable. Also remove "
            "QA cases used in the third-stage expert review documents for partial dialectization "
            "rules/examples and synonym rewrite examples."
        ),
        "deduplication_key": [
            "source_file + original_case",
            "source_file + source_idx fallback",
        ],
        "jailbreak_en_data_dir": "jailbreak/en_data",
        "jailbreak_en_data_summary": en_data_summary,
        "expert_review_source_code": "专家审核文件生成/src/build_expert_review_txt_files.py",
        "expert_review_documents": [
            "部分方言化的规则及示例审核.docx",
            "同义改写示例审核.docx",
        ],
        "expert_review_source_summary": expert_review_summary,
        "countries": {},
        "totals": {
            "source_records": 0,
            "removed_total": 0,
            "removed_jailbreak_en_data": 0,
            "removed_expert_dialect_review": 0,
            "removed_expert_rewrite_review": 0,
            "removed_overlap_across_sources": 0,
            "output_records": 0,
        },
    }

    for country in COUNTRIES:
        country_dir = OUTPUT_DIR / country["country_dir"]
        country_dir.mkdir(parents=True, exist_ok=True)
        country_summary = {
            "country": country["country"],
            "language": country["language"],
            "output_dir": str(country_dir.relative_to(BASE_DIR)),
            "types": {},
            "totals": {
                "source_records": 0,
                "removed_total": 0,
                "removed_jailbreak_en_data": 0,
                "removed_expert_dialect_review": 0,
                "removed_expert_rewrite_review": 0,
                "removed_overlap_across_sources": 0,
                "output_records": 0,
            },
        }

        for question_type in QUESTION_TYPES:
            filename = f"{country['prefix']}_{question_type['file_stem']}.json"
            source_path = QA_DIR / filename
            output_path = country_dir / filename
            source_file = str(source_path.relative_to(BASE_DIR))
            removal = removals.get(source_file, empty_removal())

            if question_type["structure"] == "mcq":
                output_data, source_count, output_count = filter_mcq_file(source_path, removal)
            else:
                output_data, source_count, output_count = filter_qa_list_file(source_path, removal)

            write_json(output_path, output_data)
            removed_count = source_count - output_count
            by_origin = removal_origin_counts(removal)
            expected_removed = removal_size(removal)
            if removed_count != expected_removed:
                raise ValueError(
                    f"{source_file}: removed {removed_count}, expected {expected_removed} from all removal sources"
                )
            overlap_count = sum(by_origin.values()) - removed_count

            type_summary = {
                "source_file": source_file,
                "output_file": str(output_path.relative_to(BASE_DIR)),
                "source_records": source_count,
                "removed_total": removed_count,
                "removed_jailbreak_en_data": by_origin.get("jailbreak_en_data", 0),
                "removed_expert_dialect_review": by_origin.get("expert_dialect_review", 0),
                "removed_expert_rewrite_review": by_origin.get("expert_rewrite_review", 0),
                "removed_overlap_across_sources": overlap_count,
                "removed_by_origin": by_origin,
                "output_records": output_count,
            }
            country_summary["types"][question_type["label"]] = type_summary
            for key in country_summary["totals"]:
                country_summary["totals"][key] += type_summary[key]
                summary["totals"][key] += type_summary[key]

        summary["countries"][country["country_dir"]] = country_summary

    write_json(SUMMARY_PATH, summary)
    print_summary(summary)
    return 0


def empty_removal() -> dict[str, set[str]]:
    return {
        "source_idxs": set(),
        "original_cases": set(),
        "jailbreak_en_data": set(),
        "expert_dialect_review": set(),
        "expert_rewrite_review": set(),
    }


def removal_size(removal: dict[str, set[str]]) -> int:
    return len(removal["original_cases"] or removal["source_idxs"])


def removal_origin_counts(removal: dict[str, set[str]]) -> dict[str, int]:
    return {
        origin: len(removal[origin])
        for origin in ["jailbreak_en_data", "expert_dialect_review", "expert_rewrite_review"]
        if removal[origin]
    }


def add_removal(
    removals: dict[str, dict[str, set[str]]],
    *,
    source_file: str,
    source_idx: str,
    original_case: str,
    origin: str,
) -> None:
    if not source_file or not source_idx:
        raise ValueError(f"Missing source_file/source_idx for {origin}: {source_file!r}, {source_idx!r}")
    removal = removals[source_file]
    removal["source_idxs"].add(source_idx)
    if is_qa_list_source_file(source_file) and original_case:
        key = f"case:{original_case}"
        removal["original_cases"].add(original_case)
    else:
        key = f"idx:{source_idx}"
    removal[origin].add(key)


def merge_removal_maps(
    target: dict[str, dict[str, set[str]]],
    incoming: dict[str, dict[str, set[str]]],
) -> None:
    for source_file, removal in incoming.items():
        target_removal = target.setdefault(source_file, empty_removal())
        for key, values in removal.items():
            target_removal[key].update(values)


def load_en_data_removals() -> tuple[dict[str, dict[str, set[str]]], dict[str, Any]]:
    removals: dict[str, dict[str, set[str]]] = defaultdict(empty_removal)
    summary: dict[str, Any] = {}

    for country in COUNTRIES:
        path = EN_DATA_DIR / country["en_data_file"]
        data = read_json(path)
        if not isinstance(data, list):
            raise ValueError(f"{path} must contain a JSON list")

        by_source_file = Counter()
        by_task_type = Counter()
        missing_refs = []
        for row_number, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                continue
            source_file = normalize_source_file(item.get("source_file"))
            source_idx = normalize_text(item.get("source_idx"))
            original_case = normalize_text(item.get("original_case"))
            if not source_file or not source_idx:
                missing_refs.append(row_number)
                continue
            add_removal(
                removals,
                source_file=source_file,
                source_idx=source_idx,
                original_case=original_case,
                origin="jailbreak_en_data",
            )
            by_source_file[source_file] += 1
            by_task_type[normalize_text(item.get("task_type_key")) or normalize_text(item.get("task_type"))] += 1

        if missing_refs:
            raise ValueError(f"{path} has records missing source_file/source_idx: {missing_refs[:10]}")

        summary[str(path.relative_to(BASE_DIR))] = {
            "records": len(data),
            "unique_source_refs": len(
                {
                    (normalize_source_file(x.get("source_file")), normalize_text(x.get("source_idx")))
                    for x in data
                    if isinstance(x, dict)
                }
            ),
            "unique_original_cases": len(
                {
                    (normalize_source_file(x.get("source_file")), normalize_text(x.get("original_case")))
                    for x in data
                    if isinstance(x, dict) and normalize_text(x.get("original_case"))
                }
            ),
            "by_source_file": dict(sorted(by_source_file.items())),
            "by_task_type": dict(sorted(by_task_type.items())),
        }

    return dict(removals), summary


def load_expert_review_removals() -> tuple[dict[str, dict[str, set[str]]], dict[str, Any]]:
    removals: dict[str, dict[str, set[str]]] = defaultdict(empty_removal)
    summary: dict[str, Any] = {
        "policy": (
            "Trace the QA originals used by build_expert_review_txt_files.py for the partial "
            "dialectization review and synonym rewrite review documents."
        ),
        "by_source_file": {},
    }

    by_source_file: dict[str, Counter[str]] = defaultdict(Counter)
    for country in COUNTRIES:
        dialect_path = LOW_RESOURCE_DIR / f"{country['language']}_safety_dialect_generalization.json"
        for item in read_json_list(dialect_path):
            source_file = normalize_source_file(item.get("source_file"))
            source_idx = normalize_text(item.get("source_idx"))
            if not source_file.startswith("QA/"):
                continue
            add_removal(
                removals,
                source_file=source_file,
                source_idx=source_idx,
                original_case=normalize_text(item.get("original")),
                origin="expert_dialect_review",
            )
            by_source_file[source_file]["expert_dialect_review"] += 1

    for item in read_json_list(QA_REWRITE_PATH):
        if item.get("country") not in {country["country"] for country in COUNTRIES}:
            continue
        source_file = normalize_source_file(item.get("source_file"))
        source_idx = normalize_text(item.get("source_idx"))
        if not source_file.startswith("QA/"):
            continue
        add_removal(
            removals,
            source_file=source_file,
            source_idx=source_idx,
            original_case=normalize_text(item.get("original")),
            origin="expert_rewrite_review",
        )
        by_source_file[source_file]["expert_rewrite_review"] += 1

    for source_file, removal in sorted(removals.items()):
        summary["by_source_file"][source_file] = {
            "unique_removal_refs": removal_size(removal),
            "by_origin": dict(sorted(by_source_file[source_file].items())),
        }

    return dict(removals), summary


def collect_available_source_idxs() -> dict[str, dict[str, set[str]]]:
    available: dict[str, dict[str, set[str]]] = {}
    for country in COUNTRIES:
        for question_type in QUESTION_TYPES:
            filename = f"{country['prefix']}_{question_type['file_stem']}.json"
            source_path = QA_DIR / filename
            source_file = str(source_path.relative_to(BASE_DIR))
            if question_type["structure"] == "mcq":
                available[source_file] = {
                    "source_idxs": collect_mcq_source_idxs(source_path),
                    "original_cases": set(),
                }
            else:
                available[source_file] = {
                    "source_idxs": collect_qa_list_source_idxs(source_path),
                    "original_cases": collect_qa_list_original_cases(source_path),
                }
    return available


def validate_removals(removals: dict[str, dict[str, set[str]]], available: dict[str, dict[str, set[str]]]) -> None:
    errors = []
    for source_file, removal in sorted(removals.items()):
        if source_file not in available:
            errors.append(f"{source_file}: source file is not part of configured QA inputs")
            continue
        if removal["original_cases"]:
            missing_cases = removal["original_cases"] - available[source_file]["original_cases"]
            if missing_cases:
                errors.append(
                    f"{source_file}: {len(missing_cases)} original_case values were not found in current QA file"
                )
        else:
            missing = sorted(removal["source_idxs"] - available[source_file]["source_idxs"], key=source_idx_sort_key)
            if missing:
                errors.append(f"{source_file}: source_idx not found in current QA file: {missing[:20]}")
    if errors:
        raise ValueError("\n".join(errors))


def filter_qa_list_file(path: Path, removal: dict[str, set[str]]) -> tuple[list[dict[str, Any]], int, int]:
    data = read_json(path)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")

    remove_cases = removal["original_cases"]
    remove_idxs = set() if remove_cases else removal["source_idxs"]
    output = []
    for row_number, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            output.append(item)
            continue
        source_idx = normalize_text(item.get("idx")) or str(row_number)
        case = normalize_text(item.get("case"))
        if case in remove_cases or source_idx in remove_idxs:
            continue
        output.append(item)
    return output, len(data), len(output)


def filter_mcq_file(path: Path, removal: dict[str, set[str]]) -> tuple[dict[str, Any], int, int]:
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")

    remove_idxs = removal["source_idxs"]
    output = copy.deepcopy(data)
    source_count = 0
    output_count = 0
    for regulation_index, regulation in enumerate(output.get("regulations", []), start=1):
        for item_index, item in enumerate(regulation.get("items", []), start=1):
            qa_block = item.get("qa", {})
            qas = qa_block.get("qas", [])
            if not isinstance(qas, list):
                continue
            filtered_qas = []
            for qa_index, qa in enumerate(qas, start=1):
                source_count += 1
                source_idx = make_mcq_source_idx(regulation_index, item_index, qa_index)
                if source_idx in remove_idxs:
                    continue
                filtered_qas.append(qa)
            qa_block["qas"] = filtered_qas
            output_count += len(filtered_qas)
    return output, source_count, output_count


def collect_qa_list_source_idxs(path: Path) -> set[str]:
    data = read_json(path)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return {
        normalize_text(item.get("idx")) or str(row_number)
        for row_number, item in enumerate(data, start=1)
        if isinstance(item, dict)
    }


def collect_qa_list_original_cases(path: Path) -> set[str]:
    data = read_json(path)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return {
        normalize_text(item.get("case"))
        for item in data
        if isinstance(item, dict) and normalize_text(item.get("case"))
    }


def collect_mcq_source_idxs(path: Path) -> set[str]:
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    source_idxs = set()
    for regulation_index, regulation in enumerate(data.get("regulations", []), start=1):
        for item_index, item in enumerate(regulation.get("items", []), start=1):
            qas = item.get("qa", {}).get("qas", [])
            if not isinstance(qas, list):
                continue
            for qa_index, _qa in enumerate(qas, start=1):
                source_idxs.add(make_mcq_source_idx(regulation_index, item_index, qa_index))
    return source_idxs


def make_mcq_source_idx(regulation_index: int, item_index: int, qa_index: int) -> str:
    return f"reg{regulation_index}_item{item_index}_qa{qa_index}"


def normalize_source_file(value: Any) -> str:
    text = normalize_text(value).replace("\\", "/")
    if text.startswith("./"):
        return text[2:]
    return text


def is_qa_list_source_file(source_file: str) -> bool:
    return "_QA_" in source_file


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def source_idx_sort_key(value: str) -> tuple[int, str]:
    return (int(value), value) if value.isdigit() else (10**9, value)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_list(path: Path) -> list[dict[str, Any]]:
    data = read_json(path)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return [item for item in data if isinstance(item, dict)]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def print_summary(summary: dict[str, Any]) -> None:
    print("country,type,source,removed_total,removed_en_data,removed_expert_dialect,removed_expert_rewrite,output")
    for country_key, country in summary["countries"].items():
        for label, item in country["types"].items():
            print(
                f"{country_key},{label},{item['source_records']},"
                f"{item['removed_total']},{item['removed_jailbreak_en_data']},"
                f"{item['removed_expert_dialect_review']},{item['removed_expert_rewrite_review']},"
                f"{item['output_records']}"
            )
        totals = country["totals"]
        print(
            f"{country_key},TOTAL,{totals['source_records']},"
            f"{totals['removed_total']},{totals['removed_jailbreak_en_data']},"
            f"{totals['removed_expert_dialect_review']},{totals['removed_expert_rewrite_review']},"
            f"{totals['output_records']}"
        )
    totals = summary["totals"]
    print(
        f"ALL,TOTAL,{totals['source_records']},"
        f"{totals['removed_total']},{totals['removed_jailbreak_en_data']},"
        f"{totals['removed_expert_dialect_review']},{totals['removed_expert_rewrite_review']},"
        f"{totals['output_records']}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
