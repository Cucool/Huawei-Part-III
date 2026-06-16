from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import all as base


NO_REWRITE_METHOD_NAMES = [
    "AutoAdv",
    "BreakFun",
    "CodeAttack",
    "DeepInception",
    "Drunk",
    "EquaCode",
    "ISC",
    "Multilingual",
    "RedQueenAttack",
]

REWRITE_METHOD_NAMES = [
    "FlipAttack",
    "JailCon",
    "MouseTrap",
    "QueryAttack",
    "RA-DRI",
    "Trojfill",
]

METHOD_NAME_ORDER = NO_REWRITE_METHOD_NAMES + REWRITE_METHOD_NAMES


def rebuild_attack_methods() -> list[base.AttackMethod]:
    original_by_name = {method.name: method for method in base.ATTACK_METHODS}
    missing = [name for name in METHOD_NAME_ORDER if name not in original_by_name]
    if missing:
        raise RuntimeError(f"all_test.py 攻击方法配置缺失: {missing}")
    return [
        base.AttackMethod(
            original_by_name[name].attack_id,
            original_by_name[name].name,
            original_by_name[name].script,
        )
        for name in METHOD_NAME_ORDER
    ]


TEST_ATTACK_METHODS = rebuild_attack_methods()
TEST_METHOD_BY_NAME = {method.name: method for method in TEST_ATTACK_METHODS}
TEST_METHOD_BY_ID = {method.attack_id: method for method in TEST_ATTACK_METHODS}
NO_REWRITE_METHODS = [TEST_METHOD_BY_NAME[name] for name in NO_REWRITE_METHOD_NAMES]
REWRITE_METHODS = [TEST_METHOD_BY_NAME[name] for name in REWRITE_METHOD_NAMES]


TRACE_PREFERRED_KEYS = (
    "source_file",
    "source_idx",
    "source_index",
    "source_id",
    "source_key",
    "source_item",
    "source_rule_id",
    "source_rule_zh",
    "original_language",
    "original_case",
    "country",
    "task_type",
    "task_type_key",
    "category_label",
    "source_data_file",
    "source_data_filename",
    "source_row_index",
    "source_original_id",
    "dispatch_group",
    "dispatch_source_language",
)


def source_language_for_item(item: dict[str, Any]) -> str:
    for key in ("original_language", "source_language", "language"):
        normalized = base.normalize_language(item.get(key, ""))
        if normalized:
            return normalized
    return "unknown"


def sort_key_for_item(item: dict[str, Any]) -> tuple[int, str]:
    try:
        return int(item.get("id")), ""
    except (TypeError, ValueError):
        return 10**12, str(item.get("id", ""))


def load_category_records(data_files: list[Path], category: str) -> list[dict[str, Any]]:
    rows: list[tuple[int, str, int, dict[str, Any]]] = []
    for path in data_files:
        for row_index, item in enumerate(base.load_json_records(path), start=1):
            try:
                sort_id = int(item.get("id"))
            except (TypeError, ValueError):
                sort_id = row_index
            rows.append((sort_id, path.name, row_index, dict(item)))

    records: list[dict[str, Any]] = []
    for category_id, (_sort_id, path_name, row_index, item) in enumerate(sorted(rows), start=1):
        raw_id = item.get("id")
        record = dict(item)
        source_language = source_language_for_item(record)
        record.update(
            {
                "id": category_id,
                "category": category,
                "case": base.extract_case_text(item),
                "language": base.normalize_language(item.get("language", "")),
                "source_data_file": path_name,
                "source_data_filename": path_name,
                "source_row_index": row_index,
                "source_original_id": raw_id,
                "dispatch_source_language": source_language,
                "dispatch_group": f"{source_language}:{category}",
            }
        )
        records.append(record)
    return records


def load_records_from_dispatch(shard_paths: dict[int, Path], category: str) -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    for path in shard_paths.values():
        for item in base.load_json_records(path):
            try:
                case_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if case_id in by_id:
                continue
            record = dict(item)
            record.update(
                {
                    "id": case_id,
                    "category": item.get("category", category),
                    "case": base.extract_case_text(item),
                    "language": base.normalize_language(item.get("language", "")),
                }
            )
            by_id[case_id] = record
    return [by_id[case_id] for case_id in sorted(by_id)]


def dispatch_row(category: str, item: dict[str, Any], method: base.AttackMethod) -> dict[str, Any]:
    row = dict(item)
    row.update(
        {
            "id": int(item["id"]),
            "attack_id": method.attack_id,
            "attack_method": method.name,
            "case": item["case"],
            "category": category,
            "language": item.get("language", ""),
        }
    )
    return row


def dispatch_category(
    *,
    category: str,
    records: list[dict[str, Any]],
    dispatch_dir: Path,
) -> dict[int, Path]:
    shards: dict[int, list[dict[str, Any]]] = {method.attack_id: [] for method in TEST_ATTACK_METHODS}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        grouped[source_language_for_item(item)].append(item)

    group_summaries: list[str] = []
    total_no_rewrite = 0
    total_remaining = 0
    for source_language in sorted(grouped):
        group_records = sorted(grouped[source_language], key=sort_key_for_item)
        no_rewrite_count = min(len(NO_REWRITE_METHODS), len(group_records))
        total_no_rewrite += no_rewrite_count

        consumed_ids: set[int] = set()
        for group_position, (method, item) in enumerate(
            zip(NO_REWRITE_METHODS, group_records[:no_rewrite_count]),
            start=1,
        ):
            item = dict(item)
            item["dispatch_group_position"] = group_position
            item["dispatch_group_size"] = len(group_records)
            shards[method.attack_id].append(dispatch_row(category, item, method))
            consumed_ids.add(int(item["id"]))

        remaining_records = [item for item in group_records if int(item["id"]) not in consumed_ids]
        total_remaining += len(remaining_records)
        for index, item in enumerate(remaining_records):
            method = REWRITE_METHODS[index % len(REWRITE_METHODS)]
            item = dict(item)
            item["dispatch_group_position"] = no_rewrite_count + index + 1
            item["dispatch_group_size"] = len(group_records)
            shards[method.attack_id].append(dispatch_row(category, item, method))

        group_summaries.append(
            f"{source_language}: 总数 {len(group_records)}，"
            f"非改写 {no_rewrite_count}，轮询 {len(remaining_records)}"
        )

    output_paths: dict[int, Path] = {}
    category_dir = dispatch_dir / category
    for method in TEST_ATTACK_METHODS:
        rows = sorted(shards[method.attack_id], key=lambda row: int(row["id"]))
        if not rows:
            continue
        path = category_dir / f"{method.attack_id:02d}_{method.name}" / f"{category}.json"
        base.save_json_atomic(rows, path)
        output_paths[method.attack_id] = path

    print(
        f"[{category}][all_test] 已整合 {len(records)} 条；"
        f"按 {len(grouped)} 个语言组分别分发；"
        f"非改写样本 {total_no_rewrite} 条；"
        f"轮询样本 {total_remaining} 条。"
    )
    for summary in group_summaries:
        print(f"[{category}][all_test] {summary}")
    return output_paths


def merge_trace_fields(record: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    merged = dict(record)
    for key in TRACE_PREFERRED_KEYS:
        if key in source and key not in merged:
            merged[key] = source[key]
    for key, value in source.items():
        if key.startswith("source_") or key.startswith("dispatch_"):
            merged.setdefault(key, value)
    return merged


def aggregate_category_results(
    *,
    model_name: str,
    category: str,
    expected_records: list[dict[str, Any]],
    output_root: Path,
) -> Path:
    model_tag = base.sanitize_model_name(model_name)
    combined_by_id: dict[int, dict[str, Any]] = {}
    expected_by_id = {int(item["id"]): item for item in expected_records}

    for method in base.ATTACK_METHODS:
        path = output_root / f"attack_{method.name}" / model_tag / f"{category}.json"
        if not path.exists():
            print(f"⚠️ [{category}] 缺少攻击结果: {path}")
            continue
        for item in base.load_json_records(path):
            try:
                case_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            expected = expected_by_id.get(case_id)
            if expected is None:
                continue
            if str(item.get("case", "")).strip() != str(expected.get("case", "")).strip():
                continue
            combined_by_id[case_id] = merge_trace_fields(dict(item), expected)

    missing_ids = sorted({int(item["id"]) for item in expected_records} - set(combined_by_id))
    if missing_ids:
        print(f"⚠️ [{category}] 汇总缺失 {len(missing_ids)} 条结果，前 20 个 id: {missing_ids[:20]}")

    model_dir = output_root / model_tag
    model_dir.mkdir(parents=True, exist_ok=True)
    output_path = model_dir / f"{category}.json"
    base.save_json_atomic([combined_by_id[i] for i in sorted(combined_by_id)], output_path)
    print(f"[{model_name}][{category}] 攻击结果汇总 -> {output_path}")
    return output_path


_BASE_JUDGE_CATEGORY_FILE = base.judge_category_file


def judge_category_file_with_trace(
    *,
    model_name: str,
    category: str,
    result_path: Path,
    output_root: Path,
    judge_model: str,
    judge_workers: int,
) -> dict[str, Any]:
    summary = _BASE_JUDGE_CATEGORY_FILE(
        model_name=model_name,
        category=category,
        result_path=result_path,
        output_root=output_root,
        judge_model=judge_model,
        judge_workers=judge_workers,
    )
    judge_path = Path(summary.get("judge_path") or output_root / base.sanitize_model_name(model_name) / f"{category}_judge.json")
    if not judge_path.exists() or not result_path.exists():
        return summary

    result_by_id: dict[int, dict[str, Any]] = {}
    for item in base.load_json_records(result_path):
        try:
            result_by_id[int(item.get("id"))] = item
        except (TypeError, ValueError):
            continue

    changed = False
    judged_rows: list[dict[str, Any]] = []
    for item in base.load_json_records(judge_path):
        try:
            source = result_by_id.get(int(item.get("id")))
        except (TypeError, ValueError):
            source = None
        if source:
            merged = merge_trace_fields(item, source)
            changed = changed or merged != item
            judged_rows.append(merged)
        else:
            judged_rows.append(item)
    if changed:
        base.save_json_atomic(judged_rows, judge_path)
        print(f"[Judge][{model_name}][{category}] 已补充溯源字段 -> {judge_path}")
    return summary


def install_overrides() -> None:
    base.ATTACK_METHODS = TEST_ATTACK_METHODS
    base.METHOD_BY_ID = TEST_METHOD_BY_ID
    base.METHOD_BY_NAME = {method.name.lower(): method for method in TEST_ATTACK_METHODS}
    if "JailCon" in TEST_METHOD_BY_NAME:
        base.METHOD_BY_NAME["hill"] = TEST_METHOD_BY_NAME["JailCon"]
        base.METHOD_BY_NAME["jail-con"] = TEST_METHOD_BY_NAME["JailCon"]
    if "RA-DRI" in TEST_METHOD_BY_NAME:
        base.METHOD_BY_NAME["radri"] = TEST_METHOD_BY_NAME["RA-DRI"]
        base.METHOD_BY_NAME["ra_dri"] = TEST_METHOD_BY_NAME["RA-DRI"]
        base.METHOD_BY_NAME["pasttenseattack"] = TEST_METHOD_BY_NAME["RA-DRI"]
        base.METHOD_BY_NAME["pasttense"] = TEST_METHOD_BY_NAME["RA-DRI"]
    base.load_category_records = load_category_records
    base.load_records_from_dispatch = load_records_from_dispatch
    base.dispatch_category = dispatch_category
    base.aggregate_category_results = aggregate_category_results
    base.judge_category_file = judge_category_file_with_trace
    os.environ.setdefault("MALICIOUS_QUERY_LANGUAGE_CHECK", "warn")


def main(argv: list[str] | None = None) -> None:
    install_overrides()
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["run", *argv]
    if argv and argv[0] == "list-methods":
        print(
            json.dumps(
                [
                    {
                        "attack_id": method.attack_id,
                        "attack_method": method.name,
                        "script": str(method.script),
                    }
                    for method in TEST_ATTACK_METHODS
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    base.main(argv)


if __name__ == "__main__":
    main()
