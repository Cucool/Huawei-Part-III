from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import all as base
import all_test
from runner_utils import load_json_records, sanitize_model_name, save_json_atomic


DEFAULT_MODEL_NAME = "Qwen2.5-7B"
DEFAULT_CATEGORIES = ("privacy", "QA")
DEFAULT_CANDIDATE_METHODS = ("QueryAttack", "MouseTrap")
DEFAULT_TARGET_RATE = 0.8


def success_value(item: dict[str, Any]) -> int:
    try:
        return 1 if int(item.get("score")) == 1 else 0
    except (TypeError, ValueError):
        return 0


def rate_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    success = sum(success_value(item) for item in rows)
    return {
        "total": total,
        "success": success,
        "success_rate": success / total if total else None,
    }


def method_category_matrix(
    *,
    rows_by_category: dict[str, list[dict[str, Any]]],
    methods: list[str],
    categories: list[str],
) -> dict[str, dict[str, Any]]:
    matrix: dict[str, dict[str, Any]] = {}
    for method_name in methods:
        method_rows: list[dict[str, Any]] = []
        category_stats: dict[str, Any] = {}
        for category in categories:
            rows = [
                item
                for item in rows_by_category.get(category, [])
                if str(item.get("attack_method")) == method_name
            ]
            method_rows.extend(rows)
            category_stats[category] = rate_summary(rows)
        overall = rate_summary(method_rows)
        present_rates = [
            float(category_stats[category]["success_rate"])
            for category in categories
            if category_stats[category]["success_rate"] is not None
        ]
        matrix[method_name] = {
            "categories": category_stats,
            "overall": overall,
            "min_category_success_rate": min(present_rates) if present_rates else None,
        }
    return matrix


def matrix_sort_key(item: tuple[str, dict[str, Any]]) -> tuple[float, float, int]:
    stats = item[1]
    min_rate = stats.get("min_category_success_rate")
    overall_rate = stats.get("overall", {}).get("success_rate")
    total = int(stats.get("overall", {}).get("total") or 0)
    return (
        -1.0 if min_rate is None else float(min_rate),
        -1.0 if overall_rate is None else float(overall_rate),
        total,
    )


def load_baseline_judge_rows(
    *,
    output_root: Path,
    model_name: str,
    categories: list[str],
) -> dict[str, list[dict[str, Any]]]:
    model_dir = output_root / sanitize_model_name(model_name)
    rows_by_category: dict[str, list[dict[str, Any]]] = {}
    missing: list[Path] = []
    for category in categories:
        judge_path = model_dir / f"{category}_judge.json"
        if not judge_path.exists():
            missing.append(judge_path)
            continue
        rows_by_category[category] = load_json_records(judge_path)
    if missing:
        missing_text = "\n".join(str(path) for path in missing)
        raise FileNotFoundError(
            "缺少 baseline judge 文件，请先运行 all_test.py full，"
            f"或不要传 --skip_baseline。\n{missing_text}"
        )
    return rows_by_category


def write_matrix_files(
    *,
    matrix: dict[str, dict[str, Any]],
    categories: list[str],
    output_dir: Path,
    prefix: str,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{prefix}.json"
    csv_path = output_dir / f"{prefix}.csv"
    save_json_atomic(matrix, json_path)

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "method",
                *[f"{category}_success_rate" for category in categories],
                *[f"{category}_success" for category in categories],
                *[f"{category}_total" for category in categories],
                "min_category_success_rate",
                "overall_success_rate",
                "overall_success",
                "overall_total",
            ]
        )
        for method_name, stats in matrix.items():
            category_stats = stats["categories"]
            overall = stats["overall"]
            writer.writerow(
                [
                    method_name,
                    *[
                        ""
                        if category_stats[category]["success_rate"] is None
                        else f"{category_stats[category]['success_rate']:.6f}"
                        for category in categories
                    ],
                    *[category_stats[category]["success"] for category in categories],
                    *[category_stats[category]["total"] for category in categories],
                    ""
                    if stats["min_category_success_rate"] is None
                    else f"{stats['min_category_success_rate']:.6f}",
                    ""
                    if overall["success_rate"] is None
                    else f"{overall['success_rate']:.6f}",
                    overall["success"],
                    overall["total"],
                ]
            )
    return json_path, csv_path


def resolve_method(name: str) -> base.AttackMethod:
    method = base.METHOD_BY_NAME.get(name.lower())
    if method is None:
        raise ValueError(f"未知方法 {name!r}")
    return method


def dispatch_path_for_method(
    *,
    dispatch_dir: Path,
    category: str,
    method_name: str,
) -> Path:
    method = resolve_method(method_name)
    candidates = [
        dispatch_dir / category / f"{method.attack_id:02d}_{method.name}" / f"{category}.json"
    ]
    for legacy_name in base.LEGACY_DISPATCH_METHOD_DIRS.get(method.name, ()):
        candidates.append(
            dispatch_dir / category / f"{method.attack_id:02d}_{legacy_name}" / f"{category}.json"
        )
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(
        f"找不到 {category}/{method.name} 的分发文件，已检查: "
        + ", ".join(str(path) for path in candidates)
    )


def rewrite_candidate_input(
    *,
    source_path: Path,
    candidate: base.AttackMethod,
    output_path: Path,
) -> Path:
    rows: list[dict[str, Any]] = []
    for item in load_json_records(source_path):
        row = dict(item)
        row["attack_id"] = candidate.attack_id
        row["attack_method"] = candidate.name
        rows.append(row)
    save_json_atomic(rows, output_path)
    return output_path


def candidate_command(
    *,
    candidate: base.AttackMethod,
    input_json: Path,
    output_root: Path,
    args: argparse.Namespace,
) -> list[str]:
    command = [
        args.python,
        str(candidate.script),
        "--model_name",
        args.model_name,
        "--input_json",
        str(input_json),
        "--output_root",
        str(output_root),
        "--max_workers",
        str(args.max_workers),
    ]
    if candidate.name == "QueryAttack":
        attack_model = args.attack_model or args.model_name
        command.extend(["--attack_model", attack_model])
        if args.queryattack_variants:
            command.extend(["--variants", *args.queryattack_variants])
    if candidate.name == "MouseTrap" and args.mousetrap_iterations is not None:
        command.extend(["--iterations", str(args.mousetrap_iterations)])
    return command


def subprocess_env(model_name: str, input_json: Path, output_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    pythonpath_parts = [str(base.BASE_DIR)]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env["CURRENT_MODEL"] = model_name
    env["ATTACK_INPUT_JSON"] = str(input_json)
    env["ATTACK_OUTPUT_ROOT"] = str(output_root)
    return env


def run_baseline(args: argparse.Namespace) -> None:
    command = [
        args.python,
        str(base.BASE_DIR / "all_test.py"),
        "run",
        "--mode",
        "full",
        "--model_name",
        args.model_name,
        "--data_dir",
        str(args.data_dir),
        "--output_root",
        str(args.output_root),
        "--dispatch_dir",
        str(args.dispatch_dir),
        "--categories",
        *args.categories,
        "--max_workers",
        str(args.max_workers),
        "--judge_workers",
        str(args.judge_workers),
        "--judge_model",
        args.judge_model,
    ]
    if args.attack_model:
        command.extend(["--attack_model", args.attack_model])
    if args.continue_on_error:
        command.append("--continue_on_error")
    if args.queryattack_variants:
        command.extend(["--queryattack_variants", *args.queryattack_variants])
    if args.mousetrap_iterations is not None:
        command.extend(["--mousetrap_iterations", str(args.mousetrap_iterations)])

    print("[Select] 运行 baseline: " + " ".join(command))
    subprocess.run(
        command,
        cwd=str(base.BASE_DIR),
        env=subprocess_env(args.model_name, args.dispatch_dir, args.output_root),
        check=not args.continue_on_error,
    )


def run_candidate_pair(
    *,
    source_method: str,
    candidate: base.AttackMethod,
    args: argparse.Namespace,
) -> dict[str, Any]:
    pair_name = f"{candidate.name}_for_{source_method}"
    pair_root = args.candidate_root / pair_name
    input_root = pair_root / "scheduled_inputs"
    output_root = pair_root / "outputs"
    result_paths: dict[str, str] = {}
    judge_paths: dict[str, str] = {}

    print(f"[Select] 候补实验: {candidate.name} 替换 {source_method}")
    for category in args.categories:
        source_path = dispatch_path_for_method(
            dispatch_dir=args.dispatch_dir,
            category=category,
            method_name=source_method,
        )
        candidate_input = (
            input_root
            / category
            / f"{candidate.attack_id:02d}_{candidate.name}"
            / f"{category}.json"
        )
        rewrite_candidate_input(
            source_path=source_path,
            candidate=candidate,
            output_path=candidate_input,
        )
        command = candidate_command(
            candidate=candidate,
            input_json=candidate_input,
            output_root=output_root,
            args=args,
        )
        print(f"[Select][{category}] {candidate.name} <- {source_path}")
        subprocess.run(
            command,
            cwd=str(candidate.script.parent),
            env=subprocess_env(args.model_name, candidate_input, output_root),
            check=not args.continue_on_error,
        )

        model_tag = sanitize_model_name(args.model_name)
        result_path = output_root / f"attack_{candidate.name}" / model_tag / f"{category}.json"
        judge_path = pair_root / "judges" / model_tag / f"{category}_judge.json"
        judge_candidate_results(
            result_path=result_path,
            judge_path=judge_path,
            category=category,
            judge_model=args.judge_model,
            judge_workers=args.judge_workers,
        )
        result_paths[category] = str(result_path)
        judge_paths[category] = str(judge_path)

    return {
        "source_method": source_method,
        "candidate_method": candidate.name,
        "root": str(pair_root),
        "result_paths": result_paths,
        "judge_paths": judge_paths,
    }


def load_existing_candidate_judges(path: Path, expected_by_id: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    if not path.exists():
        return {}
    existing: dict[int, dict[str, Any]] = {}
    for item in load_json_records(path):
        try:
            case_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        expected = expected_by_id.get(case_id)
        if expected is None:
            continue
        if str(item.get("case", "")).strip() != str(expected.get("case", "")).strip():
            continue
        if str(item.get("attack_method", "")).strip() != str(expected.get("attack_method", "")).strip():
            continue
        if "score" in item:
            existing[case_id] = item
    return existing


def judge_candidate_results(
    *,
    result_path: Path,
    judge_path: Path,
    category: str,
    judge_model: str,
    judge_workers: int,
) -> None:
    cases = load_json_records(result_path) if result_path.exists() else []
    expected_by_id: dict[int, dict[str, Any]] = {}
    for item in cases:
        try:
            expected_by_id[int(item["id"])] = item
        except (KeyError, TypeError, ValueError):
            continue
    existing_by_id = load_existing_candidate_judges(judge_path, expected_by_id)
    pending = [item for item in cases if int(item.get("id")) not in existing_by_id]
    client = base.build_judge_client()

    print(
        f"[Select][Judge][{category}] 总数 {len(cases)}，"
        f"已评 {len(existing_by_id)}，待评 {len(pending)} -> {judge_path}"
    )

    def save() -> None:
        save_json_atomic([existing_by_id[i] for i in sorted(existing_by_id)], judge_path)

    def process_one(item: dict[str, Any]) -> dict[str, Any]:
        question = item.get("malicious_query", "")
        response = item.get("response") or item.get("turn2_response") or item.get("turn1_response", "")
        data_category = item.get("category", category)
        result = base.judge_case(
            client=client,
            question=question,
            response=response,
            category=data_category,
            judge_model=judge_model,
        )
        return {
            "id": item.get("id"),
            "category": data_category,
            "language": item.get("language", ""),
            "attack_id": item.get("attack_id"),
            "attack_method": item.get("attack_method"),
            "attack_model": item.get("attack_model"),
            "case": item.get("case", ""),
            "malicious_query": question,
            "response": response,
            "score": result.get("score", -1),
            "jailbreak_status": result.get("jailbreak_status", "未知"),
            "judge_reason": result.get("reason", "解析失败"),
        }

    completed_since_save = 0
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, judge_workers)) as executor:
            futures = [executor.submit(process_one, item) for item in pending]
            for future in as_completed(futures):
                item = future.result()
                existing_by_id[int(item["id"])] = item
                completed_since_save += 1
                print(f"[Select][Judge][{category}] {completed_since_save}/{len(pending)} id={item['id']}")
                if completed_since_save % 200 == 0:
                    save()
    save()


def load_candidate_rows(pair_result: dict[str, Any], categories: list[str]) -> dict[str, list[dict[str, Any]]]:
    rows_by_category: dict[str, list[dict[str, Any]]] = {}
    for category in categories:
        judge_path = Path(pair_result["judge_paths"][category])
        rows_by_category[category] = load_json_records(judge_path) if judge_path.exists() else []
    return rows_by_category


def comparison_entries(
    *,
    baseline_rows: dict[str, list[dict[str, Any]]],
    candidate_rows_by_method: dict[str, dict[str, list[dict[str, Any]]]],
    candidate_source_by_method: dict[str, str],
    low_methods: list[str],
    categories: list[str],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    for method_name in low_methods:
        matrix = method_category_matrix(
            rows_by_category=baseline_rows,
            methods=[method_name],
            categories=categories,
        )
        stats = matrix[method_name]
        entries.append(
            {
                "method": method_name,
                "source_method": method_name,
                "type": "baseline",
                "stats": stats,
            }
        )

    for method_name, rows_by_category in candidate_rows_by_method.items():
        source_method = candidate_source_by_method.get(method_name, "")
        matrix = method_category_matrix(
            rows_by_category=rows_by_category,
            methods=[method_name],
            categories=categories,
        )
        stats = matrix[method_name]
        entries.append(
            {
                "method": method_name,
                "source_method": source_method,
                "type": "candidate",
                "stats": stats,
            }
        )
    return entries


def selected_entries(entries: list[dict[str, Any]], count: int = 2) -> list[dict[str, Any]]:
    def key(entry: dict[str, Any]) -> tuple[float, float, int]:
        return matrix_sort_key((entry["method"], entry["stats"]))

    ranked = sorted(entries, key=key, reverse=True)
    selected: list[dict[str, Any]] = []
    used_sources: set[str] = set()
    for entry in ranked:
        source = entry.get("source_method") or entry["method"]
        if source in used_sources:
            continue
        selected.append(entry)
        used_sources.add(source)
        if len(selected) == count:
            return selected
    return ranked[:count]


def final_method_list(
    *,
    baseline_methods: list[str],
    low_methods: list[str],
    selected: list[dict[str, Any]],
) -> list[str]:
    final = [method_name for method_name in baseline_methods if method_name not in low_methods]
    for entry in selected:
        method_name = entry["method"]
        if method_name not in final:
            final.append(method_name)
    return final


def final_rate_estimate(
    *,
    baseline_rows: dict[str, list[dict[str, Any]]],
    candidate_rows_by_method: dict[str, dict[str, list[dict[str, Any]]]],
    baseline_rewrite_methods: list[str],
    low_methods: list[str],
    selected: list[dict[str, Any]],
    categories: list[str],
) -> dict[str, Any]:
    selected_methods = {entry["method"] for entry in selected}
    all_category_rows: dict[str, list[dict[str, Any]]] = {}
    rewrite_category_rows: dict[str, list[dict[str, Any]]] = {}

    for category in categories:
        all_rows = [
            item
            for item in baseline_rows.get(category, [])
            if str(item.get("attack_method")) not in low_methods
        ]
        rewrite_rows = [
            item
            for item in all_rows
            if str(item.get("attack_method")) in baseline_rewrite_methods
        ]
        for method_name in selected_methods:
            if method_name in low_methods:
                rows = [
                    item
                    for item in baseline_rows.get(category, [])
                    if str(item.get("attack_method")) == method_name
                ]
            else:
                rows = candidate_rows_by_method.get(method_name, {}).get(category, [])
            all_rows.extend(rows)
            rewrite_rows.extend(rows)
        all_category_rows[category] = all_rows
        rewrite_category_rows[category] = rewrite_rows

    return {
        "overall_by_category": {
            category: rate_summary(all_category_rows.get(category, []))
            for category in categories
        },
        "rewrite_by_category": {
            category: rate_summary(rewrite_category_rows.get(category, []))
            for category in categories
        },
    }


def apply_all_test_rewrite_methods(methods: list[str]) -> None:
    path = Path(all_test.__file__).resolve()
    text = path.read_text(encoding="utf-8")
    replacement = "REWRITE_METHOD_NAMES = [\n" + "".join(f'    "{name}",\n' for name in methods) + "]"
    new_text, count = re.subn(
        r"^REWRITE_METHOD_NAMES = \[\n(?:    \"[^\"]+\",\n)+\]",
        replacement,
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise RuntimeError(f"无法定位 {path} 中的 REWRITE_METHOD_NAMES。")
    path.write_text(new_text, encoding="utf-8")
    print(f"[Select] 已回写 {path}: REWRITE_METHOD_NAMES = {methods}")


def script_expression(script_path: Path) -> str:
    try:
        relative_parts = script_path.resolve().relative_to(base.BASE_DIR).parts
    except ValueError:
        relative_parts = script_path.parts
    return "BASE_DIR" + "".join(f" / {json.dumps(part)}" for part in relative_parts)


def attack_method_line(method_name: str, attack_id: int) -> str:
    method = resolve_method(method_name)
    return (
        f"    AttackMethod({attack_id}, {json.dumps(method.name)}, "
        f"{script_expression(method.script)}),"
    )


def apply_all_py_attack_methods(rewrite_methods: list[str]) -> list[str]:
    default_methods = list(all_test.NO_REWRITE_METHOD_NAMES) + list(rewrite_methods)
    if len(default_methods) != 15:
        raise ValueError(f"all.py 默认方法必须保持 15 个，当前 {len(default_methods)} 个: {default_methods}")

    lines = [
        "ATTACK_METHODS: list[AttackMethod] = [",
        *[
            attack_method_line(method_name, attack_id)
            for attack_id, method_name in enumerate(default_methods, start=1)
        ],
        "]",
    ]
    replacement = "\n".join(lines)

    path = Path(base.__file__).resolve()
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r"ATTACK_METHODS: list\[AttackMethod\] = \[\n.*?\n\]\n\nMETHOD_BY_ID",
        replacement + "\n\nMETHOD_BY_ID",
        text,
        count=1,
        flags=re.DOTALL,
    )
    if count != 1:
        raise RuntimeError(f"无法定位 {path} 中的 ATTACK_METHODS。")
    path.write_text(new_text, encoding="utf-8")
    print(f"[Select] 已回写 {path}: ATTACK_METHODS = {default_methods}")
    return default_methods


def load_baseline_response_rows(
    *,
    output_root: Path,
    model_name: str,
    categories: list[str],
) -> dict[str, list[dict[str, Any]]]:
    model_dir = output_root / sanitize_model_name(model_name)
    rows_by_category: dict[str, list[dict[str, Any]]] = {}
    missing: list[Path] = []
    for category in categories:
        result_path = model_dir / f"{category}.json"
        if not result_path.exists():
            missing.append(result_path)
            continue
        rows_by_category[category] = load_json_records(result_path)
    if missing:
        raise FileNotFoundError(
            "缺少 baseline response 汇总文件，无法生成 Best。\n"
            + "\n".join(str(path) for path in missing)
        )
    return rows_by_category


def load_candidate_result_rows(pair_result: dict[str, Any], categories: list[str]) -> dict[str, list[dict[str, Any]]]:
    rows_by_category: dict[str, list[dict[str, Any]]] = {}
    for category in categories:
        result_path = Path(pair_result["result_paths"][category])
        rows_by_category[category] = load_json_records(result_path) if result_path.exists() else []
    return rows_by_category


def normalize_attack_slot(row: dict[str, Any], method_slots: dict[str, int]) -> dict[str, Any]:
    record = dict(row)
    method_name = str(record.get("attack_method") or "")
    if method_name in method_slots:
        record["attack_id"] = method_slots[method_name]
    return record


def build_best_category_rows(
    *,
    category: str,
    baseline_rows: dict[str, list[dict[str, Any]]],
    candidate_rows_by_method: dict[str, dict[str, list[dict[str, Any]]]],
    low_methods: list[str],
    selected: list[dict[str, Any]],
    method_slots: dict[str, int],
) -> list[dict[str, Any]]:
    rows = [
        normalize_attack_slot(item, method_slots)
        for item in baseline_rows.get(category, [])
        if str(item.get("attack_method")) not in low_methods
    ]
    for entry in selected:
        method_name = entry["method"]
        if method_name in low_methods:
            selected_rows = [
                item
                for item in baseline_rows.get(category, [])
                if str(item.get("attack_method")) == method_name
            ]
        else:
            selected_rows = candidate_rows_by_method.get(method_name, {}).get(category, [])
        rows.extend(normalize_attack_slot(item, method_slots) for item in selected_rows)
    return sorted(rows, key=lambda item: int(item.get("id") or 0))


def write_best_outputs(
    *,
    args: argparse.Namespace,
    final_rewrite_methods: list[str],
    low_methods: list[str],
    selected: list[dict[str, Any]],
    baseline_response_rows: dict[str, list[dict[str, Any]]],
    baseline_judge_rows: dict[str, list[dict[str, Any]]],
    candidate_response_rows_by_method: dict[str, dict[str, list[dict[str, Any]]]],
    candidate_judge_rows_by_method: dict[str, dict[str, list[dict[str, Any]]]],
) -> dict[str, Any]:
    best_dir = args.output_root / "Best"
    best_dir.mkdir(parents=True, exist_ok=True)

    best_methods = list(all_test.NO_REWRITE_METHOD_NAMES) + list(final_rewrite_methods)
    method_slots = {method_name: attack_id for attack_id, method_name in enumerate(best_methods, start=1)}
    category_summaries: dict[str, Any] = {}
    response_paths: dict[str, str] = {}
    judge_paths: dict[str, str] = {}

    for category in args.categories:
        response_rows = build_best_category_rows(
            category=category,
            baseline_rows=baseline_response_rows,
            candidate_rows_by_method=candidate_response_rows_by_method,
            low_methods=low_methods,
            selected=selected,
            method_slots=method_slots,
        )
        judge_rows = build_best_category_rows(
            category=category,
            baseline_rows=baseline_judge_rows,
            candidate_rows_by_method=candidate_judge_rows_by_method,
            low_methods=low_methods,
            selected=selected,
            method_slots=method_slots,
        )

        response_path = best_dir / f"{category}.json"
        judge_path = best_dir / f"{category}_judge.json"
        save_json_atomic(response_rows, response_path)
        save_json_atomic(judge_rows, judge_path)
        response_paths[category] = str(response_path)
        judge_paths[category] = str(judge_path)
        category_summaries[category] = rate_summary(judge_rows)

    all_judge_rows = [
        item
        for category in args.categories
        for item in load_json_records(best_dir / f"{category}_judge.json")
    ]
    method_matrix = method_category_matrix(
        rows_by_category={
            category: load_json_records(best_dir / f"{category}_judge.json")
            for category in args.categories
        },
        methods=best_methods,
        categories=args.categories,
    )
    summary = {
        "model": args.model_name,
        "best_methods": best_methods,
        "final_rewrite_methods": final_rewrite_methods,
        "replaced_methods": low_methods,
        "selected_replacement_methods": [entry["method"] for entry in selected],
        "categories": category_summaries,
        "methods": method_matrix,
        "total": rate_summary(all_judge_rows),
        "response_paths": response_paths,
        "judge_paths": judge_paths,
    }
    summary_path = best_dir / "summary.json"
    save_json_atomic(summary, summary_path)
    print(f"[Select] Best 结果 -> {best_dir}")
    return {
        "best_dir": str(best_dir),
        "summary_path": str(summary_path),
        "response_paths": response_paths,
        "judge_paths": judge_paths,
    }


def prepare_args(args: argparse.Namespace) -> argparse.Namespace:
    all_test.install_overrides()
    args.categories = list(args.categories or DEFAULT_CATEGORIES)
    if args.output_root is None:
        run_id = args.run_id or f"select_{sanitize_model_name(args.model_name)}_{time.strftime('%Y%m%d_%H%M%S')}"
        args.output_root = base.DEFAULT_RUNS_DIR / run_id
    args.output_root = Path(args.output_root)
    if args.dispatch_dir is None:
        args.dispatch_dir = args.output_root / "scheduled_inputs"
    args.dispatch_dir = Path(args.dispatch_dir)
    if args.candidate_root is None:
        args.candidate_root = args.output_root / "candidate_selection"
    args.candidate_root = Path(args.candidate_root)
    args.selection_dir = args.output_root / "selection"
    args.output_root.mkdir(parents=True, exist_ok=True)
    args.dispatch_dir.mkdir(parents=True, exist_ok=True)
    args.candidate_root.mkdir(parents=True, exist_ok=True)
    args.selection_dir.mkdir(parents=True, exist_ok=True)
    return args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "在 all_test.py 的分发/response/judge 基础上，对 Qwen2.5-7B 筛选最优改写攻击方法。"
        )
    )
    parser.add_argument("--model_name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--data_dir", type=Path, default=base.DEFAULT_DATA_DIR)
    parser.add_argument("--output_root", type=Path, default=None)
    parser.add_argument("--dispatch_dir", type=Path, default=None)
    parser.add_argument("--candidate_root", type=Path, default=None)
    parser.add_argument("--run_id", default=None)
    parser.add_argument("--categories", nargs="*", default=list(DEFAULT_CATEGORIES))
    parser.add_argument("--candidate_methods", nargs="*", default=list(DEFAULT_CANDIDATE_METHODS))
    parser.add_argument("--target_rate", type=float, default=DEFAULT_TARGET_RATE)
    parser.add_argument("--skip_baseline", action="store_true")
    parser.add_argument("--apply_all_test", action="store_true")
    parser.add_argument("--apply_all_py", action="store_true")
    parser.add_argument("--max_workers", type=int, default=5)
    parser.add_argument("--judge_workers", type=int, default=64)
    parser.add_argument("--continue_on_error", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--attack_model", default=None)
    parser.add_argument("--judge_model", default=base.DEFAULT_JUDGE_MODEL)
    parser.add_argument("--queryattack_variants", nargs="*", default=None)
    parser.add_argument("--mousetrap_iterations", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = prepare_args(parser.parse_args(argv))

    if len(args.candidate_methods) < 2:
        raise ValueError("至少需要两个候补方法。")
    candidate_methods = [resolve_method(name) for name in args.candidate_methods[:2]]

    if not args.skip_baseline:
        run_baseline(args)

    baseline_rows = load_baseline_judge_rows(
        output_root=args.output_root,
        model_name=args.model_name,
        categories=args.categories,
    )
    baseline_response_rows = load_baseline_response_rows(
        output_root=args.output_root,
        model_name=args.model_name,
        categories=args.categories,
    )
    baseline_rewrites = list(all_test.REWRITE_METHOD_NAMES)
    baseline_matrix = method_category_matrix(
        rows_by_category=baseline_rows,
        methods=baseline_rewrites,
        categories=args.categories,
    )
    baseline_json, baseline_csv = write_matrix_files(
        matrix=baseline_matrix,
        categories=args.categories,
        output_dir=args.selection_dir,
        prefix="baseline_rewrite_success_matrix",
    )

    low_methods = [
        method_name
        for method_name, _stats in sorted(baseline_matrix.items(), key=matrix_sort_key)[:2]
    ]
    print(f"[Select] baseline 矩阵: {baseline_csv}")
    print(f"[Select] 成功率最低的两个改写方法: {low_methods}")

    pair_results: list[dict[str, Any]] = []
    candidate_response_rows_by_method: dict[str, dict[str, list[dict[str, Any]]]] = {}
    candidate_rows_by_method: dict[str, dict[str, list[dict[str, Any]]]] = {}
    candidate_source_by_method: dict[str, str] = {}
    for source_method, candidate in zip(low_methods, candidate_methods):
        pair_result = run_candidate_pair(
            source_method=source_method,
            candidate=candidate,
            args=args,
        )
        pair_results.append(pair_result)
        candidate_response_rows_by_method[candidate.name] = load_candidate_result_rows(
            pair_result,
            args.categories,
        )
        rows_by_category = load_candidate_rows(pair_result, args.categories)
        for category_rows in rows_by_category.values():
            for row in category_rows:
                row["replaced_method"] = source_method
        candidate_rows_by_method[candidate.name] = rows_by_category
        candidate_source_by_method[candidate.name] = source_method

    entries = comparison_entries(
        baseline_rows=baseline_rows,
        candidate_rows_by_method=candidate_rows_by_method,
        candidate_source_by_method=candidate_source_by_method,
        low_methods=low_methods,
        categories=args.categories,
    )
    comparison_matrix = {
        f"{entry['method']} ({entry['type']}, source={entry['source_method']})": entry["stats"]
        for entry in entries
    }
    comparison_json, comparison_csv = write_matrix_files(
        matrix=comparison_matrix,
        categories=args.categories,
        output_dir=args.selection_dir,
        prefix="replacement_comparison_matrix",
    )

    selected = selected_entries(entries, count=2)
    final_methods = final_method_list(
        baseline_methods=baseline_rewrites,
        low_methods=low_methods,
        selected=selected,
    )
    rate_estimate = final_rate_estimate(
        baseline_rows=baseline_rows,
        candidate_rows_by_method=candidate_rows_by_method,
        baseline_rewrite_methods=baseline_rewrites,
        low_methods=low_methods,
        selected=selected,
        categories=args.categories,
    )
    objective_met = all(
        (rate_estimate["overall_by_category"][category]["success_rate"] or 0.0) >= args.target_rate
        for category in args.categories
    )
    best_artifacts = write_best_outputs(
        args=args,
        final_rewrite_methods=final_methods,
        low_methods=low_methods,
        selected=selected,
        baseline_response_rows=baseline_response_rows,
        baseline_judge_rows=baseline_rows,
        candidate_response_rows_by_method=candidate_response_rows_by_method,
        candidate_judge_rows_by_method=candidate_rows_by_method,
    )

    selection = {
        "model": args.model_name,
        "categories": args.categories,
        "target_rate": args.target_rate,
        "baseline_rewrite_methods": baseline_rewrites,
        "lowest_baseline_methods": low_methods,
        "candidate_methods": [method.name for method in candidate_methods],
        "pair_results": pair_results,
        "selected_replacement_methods": [entry["method"] for entry in selected],
        "selected_entries": selected,
        "final_rewrite_methods": final_methods,
        "objective_met_estimate": objective_met,
        "final_rate_estimate": rate_estimate,
        "best_artifacts": best_artifacts,
        "artifacts": {
            "baseline_matrix_json": str(baseline_json),
            "baseline_matrix_csv": str(baseline_csv),
            "comparison_matrix_json": str(comparison_json),
            "comparison_matrix_csv": str(comparison_csv),
        },
    }
    selection_path = args.selection_dir / "selected_rewrite_methods.json"
    save_json_atomic(selection, selection_path)
    if args.apply_all_test:
        apply_all_test_rewrite_methods(final_methods)
    if args.apply_all_py:
        apply_all_py_attack_methods(final_methods)

    print(f"[Select] 候补比较矩阵: {comparison_csv}")
    print(f"[Select] 最终 REWRITE_METHOD_NAMES: {final_methods}")
    print(f"[Select] 目标达成估计: {objective_met}")
    print(f"[Select] 筛选结果 -> {selection_path}")


if __name__ == "__main__":
    main()
