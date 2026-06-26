#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


REPO_DIR = Path(__file__).resolve().parents[2]
LOW_RESOURCE_DIR = REPO_DIR / "用例生成" / "generated_low_resource_cases"
REINFORCED_DIR = REPO_DIR / "generated_reinforced_qa_eval_cases"
METHOD_DIR = LOW_RESOURCE_DIR / "泛化方法"
DIALECT_DIR = METHOD_DIR / "方言化"
REWRITE_DIR = METHOD_DIR / "本地模型改写"
SUMMARY_FILE = METHOD_DIR / "summary.json"

COUNTRIES = [
    {
        "language": "arabic",
        "country": "沙特",
        "country_prompt": "Saudi Arabia",
        "prefix": "Saudi",
        "language_name": "Arabic",
        "rules_file": REPO_DIR / "用例生成" / "src" / "方言-沙特.py",
        "rules_var": "ARABIC_RULES",
        "script_re": re.compile(r"[\u0600-\u06FF]"),
    },
    {
        "language": "thai",
        "country": "泰国",
        "country_prompt": "Thailand",
        "prefix": "Thailand",
        "language_name": "Thai",
        "rules_file": REPO_DIR / "用例生成" / "src" / "方言.py",
        "rules_var": "THAILAND_RULES",
        "script_re": re.compile(r"[\u0E00-\u0E7F]"),
    },
    {
        "language": "turkish",
        "country": "土耳其",
        "country_prompt": "Turkey",
        "prefix": "Turkey",
        "language_name": "Turkish",
        "rules_file": REPO_DIR / "用例生成" / "src" / "方言.py",
        "rules_var": "TURKISH_RULES",
        "script_re": re.compile(r"[A-Za-zçğıİöşüÇĞİÖŞÜ]"),
    },
]

REWRITE_SYSTEM_PROMPT = """You rewrite localized evaluation questions.

Goal:
Produce exactly one rewritten variant of the input question. Preserve the original meaning, language, difficulty, answerability, and evaluation intent.

Requirements:
- Keep the same language as the original question.
- Do not add new facts, constraints, explanations, warnings, or policy commentary.
- Do not make the question safer, weaker, stronger, broader, or more detailed.
- For multiple-choice questions, keep the same option labels and the same correct answer meaning. You may rephrase the question and option wording.
- For true/false questions, preserve the truth value of the statement.
- Do not include Chinese unless the original question contains Chinese.
- Do not add translations in another language.
- Do not copy the original verbatim.
- Return strict JSON only: {"rewrite": "..."}"""

REWRITE_USER_TEMPLATE = """Country: {country}
Question type: {qa_type}
Required output language: {language_name}

Original question:
{question}

Rewrite it now."""


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate dialect and local-LLM rewrite variants for reinforced MCQ knowledge cases.")
    parser.add_argument("--base-url", default="http://localhost:11434/v1")
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--language", choices=["all", "arabic", "thai", "turkish"], default="all")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument(
        "--only-failures",
        action="store_true",
        help="Retry every non-success record. By default, only records with missing/empty rewrite text are generated.",
    )
    parser.add_argument("--skip-rewrite", action="store_true")
    parser.add_argument(
        "--continue-on-error",
        dest="continue_on_error",
        action="store_true",
        default=True,
        help="Record invalid rewrites as failures and continue. This is the default.",
    )
    parser.add_argument(
        "--stop-on-error",
        dest="continue_on_error",
        action="store_false",
        help="Stop immediately when a rewrite fails validation after all retries.",
    )
    args = parser.parse_args()

    DIALECT_DIR.mkdir(parents=True, exist_ok=True)
    REWRITE_DIR.mkdir(parents=True, exist_ok=True)
    organize_existing_method_files()

    summary: dict[str, Any] = {
        "method_dir": str(METHOD_DIR.relative_to(REPO_DIR)),
        "model": args.model,
        "base_url": args.base_url,
        "countries": {},
    }

    selected_countries = [
        country_spec for country_spec in COUNTRIES
        if args.language == "all" or country_spec["language"] == args.language
    ]

    for country_spec in selected_countries:
        cases = collect_reinforced_knowledge_cases(country_spec)
        dialect_records = generate_dialect_records(country_spec, cases)
        dialect_path = DIALECT_DIR / f"{country_spec['language']}_reinforced_mcq_dialect.json"
        write_json(dialect_path, dialect_records)

        rewrite_path = REWRITE_DIR / f"{country_spec['language']}_reinforced_mcq_qwen_rewrite.json"
        if args.skip_rewrite:
            rewrite_records = load_or_initialize_rewrite_records(country_spec, cases, rewrite_path, args.model)
        else:
            rewrite_records = generate_rewrite_records(
                country_spec=country_spec,
                cases=cases,
                output_path=rewrite_path,
                model=args.model,
                base_url=args.base_url,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
                retries=args.retries,
                batch_size=args.batch_size,
                only_failures=args.only_failures,
                continue_on_error=args.continue_on_error,
            )

        summary["countries"][country_spec["language"]] = {
            "country": country_spec["country"],
            "source_file": str(source_path(country_spec).relative_to(REPO_DIR)),
            "source_cases": len(cases),
            "dialect_output": str(dialect_path.relative_to(REPO_DIR)),
            "dialect_success": sum(1 for item in dialect_records if item["status"] == "success"),
            "dialect_no_match": sum(1 for item in dialect_records if item["status"] == "no_match"),
            "rewrite_output": str(rewrite_path.relative_to(REPO_DIR)),
            "rewrite_success": sum(1 for item in rewrite_records if item["status"] == "success"),
            "rewrite_failure": sum(1 for item in rewrite_records if item["status"] != "success"),
        }

    method_counts = summarize_method_dirs()
    summary["method_counts"] = method_counts
    write_json(SUMMARY_FILE, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def organize_existing_method_files() -> None:
    for source in sorted(LOW_RESOURCE_DIR.glob("*.json")):
        if source.name in {
            "summary.json",
            "redundancy_threshold_report.json",
            "safety_dialect_generalization_summary.json",
            "content_safety_nonoverlap_summary.json",
        }:
            continue
        if source.name.endswith("_qwen_rewrite.json"):
            target = REWRITE_DIR / source.name
        elif "_dialect" in source.stem:
            target = DIALECT_DIR / source.name
        else:
            continue
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)


def collect_reinforced_knowledge_cases(country_spec: dict[str, Any]) -> list[dict[str, Any]]:
    path = source_path(country_spec)
    data = json.loads(path.read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = []
    for regulation_index, regulation in enumerate(data.get("regulations", []), start=1):
        category = normalize_text(regulation.get("category"))
        for item_index, item in enumerate(regulation.get("items", []), start=1):
            item_category = normalize_text(item.get("category")) or category
            qas = item.get("qa", {}).get("qas", [])
            for qa_index, qa in enumerate(qas, start=1):
                if not isinstance(qa, dict) or not normalize_text(qa.get("question")):
                    continue
                source_idx = f"reg{regulation_index}_item{item_index}_qa{qa_index}"
                source_file = str(path.relative_to(REPO_DIR))
                original = format_qa_case(qa)
                option_labels = option_labels_for_case(qa)
                records.append(
                    {
                        "language": country_spec["language"],
                        "country": country_spec["country"],
                        "group": "reinforced_knowledge",
                        "task_type": "知识问答",
                        "task_type_key": "knowledge",
                        "qa_type": normalize_text(qa.get("type")),
                        "difficulty": normalize_text(qa.get("difficulty")),
                        "source_id": source_idx,
                        "source_file": source_file,
                        "source_idx": source_idx,
                        "source_key": f"{country_spec['language']}|{source_file}|{source_idx}",
                        "category": item_category,
                        "original": original,
                        "option_labels": option_labels,
                        "option_lines": option_lines_for_case(qa),
                        "answer": qa.get("answer"),
                    }
                )
    if not records:
        raise ValueError(f"No knowledge cases found in {path}")
    return records


def source_path(country_spec: dict[str, Any]) -> Path:
    prefix = country_spec["prefix"]
    return REINFORCED_DIR / prefix / f"{prefix}_MCQ_知识问答.json"


def format_qa_case(qa: dict[str, Any]) -> str:
    question = normalize_text(qa["question"])
    options = qa.get("options") or {}
    if not isinstance(options, dict) or not options:
        return question
    option_lines = [f"{label}. {normalize_text(value)}" for label, value in sorted(options.items())]
    return "\n".join([question, *option_lines])


def option_labels_for_case(qa: dict[str, Any]) -> list[str]:
    options = qa.get("options") or {}
    if not isinstance(options, dict):
        return []
    return sorted(str(label) for label in options)


def option_lines_for_case(qa: dict[str, Any]) -> list[str]:
    options = qa.get("options") or {}
    if not isinstance(options, dict) or not options:
        return []
    return [f"{label}. {normalize_text(value)}" for label, value in sorted(options.items())]


def generate_dialect_records(country_spec: dict[str, Any], cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rules = load_rules(country_spec["rules_file"], country_spec["rules_var"])
    records: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        if re.search(r"[\u4E00-\u9FFF]", case["original"]):
            records.append(
                {
                    "id": index,
                    **case_metadata(case),
                    "method": "dialect",
                    "rule_id": "",
                    "rule_description": "",
                    "dialect_rule": "",
                    "generated": "",
                    "status": "failure",
                    "error": "source case contains Chinese text",
                }
            )
            continue
        match = first_matching_rule(case["original"], rules, country_spec["script_re"])
        if match is None:
            records.append(
                {
                    "id": index,
                    **case_metadata(case),
                    "method": "dialect",
                    "rule_id": "",
                    "rule_description": "",
                    "dialect_rule": "",
                    "generated": "",
                    "status": "no_match",
                    "error": "no dialect rule changed this case",
                }
            )
            continue
        rule, generated = match
        records.append(
            {
                "id": index,
                **case_metadata(case),
                "method": "dialect",
                "rule_id": rule["id"],
                "rule_description": rule["description"],
                "dialect_rule": f"{rule['id']}: {rule['description']}",
                "generated": generated,
                "status": "success",
                "error": "",
            }
        )
    return records


def case_metadata(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "language": case["language"],
        "country": case["country"],
        "group": case["group"],
        "task_type": case["task_type"],
        "task_type_key": case["task_type_key"],
        "qa_type": case["qa_type"],
        "difficulty": case["difficulty"],
        "source_id": case["source_id"],
        "source_file": case["source_file"],
        "source_idx": case["source_idx"],
        "source_key": case["source_key"],
        "category": case["category"],
        "original": case["original"],
    }


def first_matching_rule(
    text: str,
    rules: list[dict[str, str]],
    script_re: re.Pattern[str],
) -> tuple[dict[str, str], str] | None:
    if not script_re.search(text):
        return None
    for rule in rules:
        generated = apply_rule(text, rule)
        if generated != text:
            return rule, generated
    return None


def load_or_initialize_rewrite_records(
    country_spec: dict[str, Any],
    cases: list[dict[str, Any]],
    output_path: Path,
    model: str,
) -> list[dict[str, Any]]:
    existing_by_key = {}
    if output_path.exists():
        existing = load_json_list(output_path)
        for item in existing:
            source_key = item.get("source_key")
            if not source_key:
                continue
            if source_key not in existing_by_key or rewrite_record_score(item) > rewrite_record_score(existing_by_key[source_key]):
                existing_by_key[source_key] = item
    return [
        existing_by_key.get(case["source_key"]) or initial_rewrite_record(country_spec, case, index, model)
        for index, case in enumerate(cases, start=1)
    ]


def generate_rewrite_records(
    *,
    country_spec: dict[str, Any],
    cases: list[dict[str, Any]],
    output_path: Path,
    model: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    retries: int,
    batch_size: int,
    only_failures: bool,
    continue_on_error: bool,
) -> list[dict[str, Any]]:
    records = load_or_initialize_rewrite_records(country_spec, cases, output_path, model)
    case_by_key = {case["source_key"]: case for case in cases}
    targets = select_rewrite_targets(records, only_failures=only_failures)
    if not targets:
        print(f"[{country_spec['language']}] no missing rewrite records")
        write_json(output_path, records)
        return records

    sequence = 0
    if batch_size > 1:
        for batch in chunks(targets, batch_size):
            sequence += len(batch)
            batch_cases = [case_by_key[item["source_key"]] for item in batch]
            try:
                rewrites = rewrite_batch(
                    base_url=base_url,
                    model=model,
                    country=country_spec["country_prompt"],
                    language_name=country_spec["language_name"],
                    cases=batch_cases,
                    script_re=country_spec["script_re"],
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    retries=retries,
                )
                for record, case in zip(batch, batch_cases):
                    rewrite = rewrites.get(case["source_idx"], "")
                    rewrite = repair_rewrite(case["original"], rewrite, case["option_labels"], case["option_lines"])
                    validate_rewrite(case["original"], rewrite, case["option_labels"], country_spec["script_re"])
                    rewrites[case["source_idx"]] = rewrite
                    record.update(
                        {
                            "model": model,
                            "rewrite": rewrite,
                            "status": "success",
                            "error": "",
                        }
                    )
                print(f"[{country_spec['language']} {sequence}/{len(targets)}] batch={len(batch)} rewrite=ok")
                write_json(output_path, records)
                continue
            except Exception as exc:
                print(
                    f"[{country_spec['language']} {sequence - len(batch) + 1}-{sequence}/{len(targets)}] "
                    f"batch rewrite=error, falling back to single records: {exc}",
                    file=sys.stderr,
                )

            for record, case in zip(batch, batch_cases):
                rewrite_single_record(
                    record=record,
                    case=case,
                    country_spec=country_spec,
                    output_path=output_path,
                    records=records,
                    model=model,
                    base_url=base_url,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    retries=retries,
                    continue_on_error=continue_on_error,
                    sequence=sequence,
                    total=len(targets),
                )
        return records

    for sequence, record in enumerate(targets, start=1):
        case = case_by_key[record["source_key"]]
        rewrite_single_record(
            record=record,
            case=case,
            country_spec=country_spec,
            output_path=output_path,
            records=records,
            model=model,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            retries=retries,
            continue_on_error=continue_on_error,
            sequence=sequence,
            total=len(targets),
        )
    return records


def select_rewrite_targets(records: list[dict[str, Any]], *, only_failures: bool) -> list[dict[str, Any]]:
    if only_failures:
        return [item for item in records if item.get("status") != "success"]
    return [item for item in records if rewrite_is_missing(item)]


def rewrite_is_missing(record: dict[str, Any]) -> bool:
    return not normalize_text(record.get("rewrite"))


def rewrite_record_score(record: dict[str, Any]) -> tuple[int, int]:
    return (
        1 if normalize_text(record.get("rewrite")) else 0,
        1 if record.get("status") == "success" else 0,
    )


def rewrite_single_record(
    *,
    record: dict[str, Any],
    case: dict[str, Any],
    country_spec: dict[str, Any],
    output_path: Path,
    records: list[dict[str, Any]],
    model: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    retries: int,
    continue_on_error: bool,
    sequence: int,
    total: int,
) -> None:
    try:
        rewrite = rewrite_one(
            base_url=base_url,
            model=model,
            country=country_spec["country_prompt"],
            language_name=country_spec["language_name"],
            question=case["original"],
            qa_type=case["qa_type"],
            option_labels=case["option_labels"],
            option_lines=case["option_lines"],
            script_re=country_spec["script_re"],
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            retries=retries,
        )
        record.update(
            {
                "model": model,
                "rewrite": rewrite,
                "status": "success",
                "error": "",
            }
        )
        print(f"[{country_spec['language']} {sequence}/{total}] {case['source_idx']} rewrite=ok")
    except Exception as exc:
        record.update(
            {
                "model": model,
                "rewrite": "",
                "status": "failure",
                "error": str(exc),
            }
        )
        print(f"[{country_spec['language']} {sequence}/{total}] {case['source_idx']} rewrite=error: {exc}", file=sys.stderr)
        write_json(output_path, records)
        if not continue_on_error:
            raise
    write_json(output_path, records)


def initial_rewrite_record(country_spec: dict[str, Any], case: dict[str, Any], index: int, model: str) -> dict[str, Any]:
    return {
        "id": index,
        **case_metadata(case),
        "method": "qwen_rewrite",
        "model": model,
        "rewrite": "",
        "status": "pending",
        "error": "",
    }


def rewrite_one(
    *,
    base_url: str,
    model: str,
    country: str,
    language_name: str,
    question: str,
    qa_type: str,
    option_labels: list[str],
    option_lines: list[str],
    script_re: re.Pattern[str],
    temperature: float,
    max_tokens: int,
    timeout: float,
    retries: int,
) -> str:
    last_error: Exception | None = None
    for _ in range(max(1, retries)):
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": REWRITE_USER_TEMPLATE.format(
                        country=country,
                        qa_type=qa_type,
                        language_name=language_name,
                        question=question,
                    ),
                },
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            data = post_json(chat_completions_url(base_url), payload, timeout)
            content = data["choices"][0]["message"].get("content") or ""
            parsed = parse_json_object(content)
            rewrite = normalize_text(str(parsed.get("rewrite") or ""))
            rewrite = repair_rewrite(question, rewrite, option_labels, option_lines)
            validate_rewrite(question, rewrite, option_labels, script_re)
            return rewrite
        except Exception as exc:
            last_error = exc
    raise ValueError(f"failed after {retries} attempts: {last_error}")


def rewrite_batch(
    *,
    base_url: str,
    model: str,
    country: str,
    language_name: str,
    cases: list[dict[str, Any]],
    script_re: re.Pattern[str],
    temperature: float,
    max_tokens: int,
    timeout: float,
    retries: int,
) -> dict[str, str]:
    batch_payload = [
        {
            "id": item["source_idx"],
            "qa_type": item["qa_type"],
            "question": item["original"],
        }
        for item in cases
    ]
    system_prompt = REWRITE_SYSTEM_PROMPT + '\n- Return strict JSON only: {"rewrites":[{"id":"...","rewrite":"..."}]}.'
    last_error: Exception | None = None
    for _ in range(max(1, retries)):
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Country: {country}\n"
                        f"Required output language: {language_name}\n\n"
                        "Rewrite each item independently. Preserve each id exactly.\n"
                        f"Items:\n{json.dumps(batch_payload, ensure_ascii=False, indent=2)}"
                    ),
                },
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            data = post_json(chat_completions_url(base_url), payload, timeout)
            content = data["choices"][0]["message"].get("content") or ""
            parsed = parse_json_object(content)
            items = parsed.get("rewrites")
            if not isinstance(items, list):
                raise ValueError(f"expected rewrites list, got: {content[:300]}")
            rewrites = {
                normalize_text(item.get("id")): normalize_text(item.get("rewrite"))
                for item in items
                if isinstance(item, dict)
            }
            for case in cases:
                rewrite = rewrites.get(case["source_idx"], "")
                rewrite = repair_rewrite(case["original"], rewrite, case["option_labels"], case["option_lines"])
                validate_rewrite(case["original"], rewrite, case["option_labels"], script_re)
                rewrites[case["source_idx"]] = rewrite
            return rewrites
        except Exception as exc:
            last_error = exc
    raise ValueError(f"batch failed after {retries} attempts: {last_error}")


def validate_rewrite(
    original: str,
    rewrite: str,
    option_labels: list[str],
    script_re: re.Pattern[str],
) -> None:
    if not rewrite:
        raise ValueError("empty rewrite from model")
    if rewrite == normalize_text(original):
        raise ValueError("model returned the original text unchanged")
    if looks_like_prompt_leak(rewrite):
        raise ValueError(f"model returned prompt text instead of rewrite: {rewrite[:300]}")
    if re.search(r"[\u4E00-\u9FFF]", rewrite):
        raise ValueError(f"rewrite contains Chinese text: {rewrite[:300]}")
    if not script_re.search(rewrite):
        raise ValueError(f"rewrite does not contain target-language script: {rewrite[:300]}")
    for label in option_labels:
        if not re.search(rf"(?<!\w){re.escape(label)}\s*[\.\):：]", rewrite):
            raise ValueError(f"rewrite is missing option label {label}: {rewrite[:300]}")


def repair_rewrite(
    original: str,
    rewrite: str,
    option_labels: list[str],
    option_lines: list[str],
) -> str:
    rewrite = normalize_text(rewrite)
    if not option_labels:
        return rewrite
    if all(re.search(rf"(?<!\w){re.escape(label)}\s*[\.\):：]", rewrite) for label in option_labels):
        return rewrite

    first_label = option_labels[0]
    first_label_match = re.search(rf"(?<!\w){re.escape(first_label)}\s*[\.\):：]", rewrite)
    stem = rewrite[: first_label_match.start()] if first_label_match else rewrite
    stem = normalize_text(stem)
    if not stem:
        return rewrite
    return normalize_text("\n".join([stem, *option_lines]))


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


def post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {body[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"request to {url} failed: {exc.reason}") from exc


def chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/") + "/"
    if normalized.endswith("/chat/completions/"):
        return normalized.rstrip("/")
    if normalized.endswith("/v1/"):
        return urljoin(normalized, "chat/completions")
    return urljoin(normalized, "v1/chat/completions")


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    start = cleaned.find("{")
    if start >= 0:
        cleaned = cleaned[start:]
    data, _ = json.JSONDecoder(strict=False).raw_decode(cleaned)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")
    return data


def looks_like_prompt_leak(text: str) -> bool:
    lowered = text.lower()
    markers = [
        "you rewrite localized evaluation questions",
        "requirements:",
        "original question:",
        "rewrite it now",
    ]
    return any(marker in lowered for marker in markers)


def summarize_method_dirs() -> dict[str, Any]:
    return {
        "dialect": summarize_json_files(DIALECT_DIR),
        "qwen_rewrite": summarize_json_files(REWRITE_DIR),
    }


def summarize_json_files(path: Path) -> list[dict[str, Any]]:
    rows = []
    for file_path in sorted(path.glob("*.json")):
        data = load_json_list(file_path)
        rows.append(
            {
                "file": str(file_path.relative_to(REPO_DIR)),
                "total": len(data),
                "success": sum(1 for item in data if item.get("status") == "success"),
                "failure_or_no_match": sum(1 for item in data if item.get("status") != "success"),
            }
        )
    return rows


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"[\s\u200b\u200c\u200d]+", " ", text).strip()


def chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    size = max(1, size)
    return [items[index : index + size] for index in range(0, len(items), size)]


def load_json_list(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return [item for item in data if isinstance(item, dict)]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise
