#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


GROUP_FILES = [
    ("dialect", "content_safety", "filtered/dialect/content_safety_dialect_kept.json"),
    ("dialect", "privacy", "filtered/dialect/privacy_dialect_kept.json"),
    ("qwen_rewrite", "content_safety", "filtered/qwen_rewrite/content_safety_qwen_rewrite_kept.json"),
    ("qwen_rewrite", "privacy", "filtered/qwen_rewrite/privacy_qwen_rewrite_kept.json"),
]

TARGET_SYSTEM_PROMPT = "You are a helpful assistant. Respond directly to the user's request."


def main() -> int:
    args = build_arg_parser().parse_args()
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    groups = load_groups(input_dir, args.limit_per_group)
    cache = JsonlOutputCache(output_dir / "seed_output_cache.jsonl")
    run_seed_outputs(args, cache, groups)
    metrics, summary = compute_metrics(args, cache, groups)
    write_json(output_dir / "seed_sigma_pair_metrics.json", metrics)
    write_json(output_dir / "seed_sigma_summary.json", summary)
    write_markdown_summary(output_dir / "seed_sigma_summary.md", summary)
    print(json.dumps({"output_dir": str(output_dir), "summary": summary}, ensure_ascii=False, indent=2))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run seed_run_1 and seed_run_2, then compute sigma only.")
    parser.add_argument("--input-dir", required=True, help="Directory containing the filtered/... kept JSON files.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible base URL, e.g. http://127.0.0.1:7000/v1")
    parser.add_argument("--model", default="Qwen2.5-7B")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--retries", type=int, default=60)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--save-every", type=int, default=100)
    parser.add_argument("--limit-per-group", type=int, default=0)
    return parser


def load_groups(input_dir: Path, limit_per_group: int) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for method, domain, relative_path in GROUP_FILES:
        path = input_dir / relative_path
        if not path.exists():
            raise FileNotFoundError(path)
        records = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError(f"{path} must contain a JSON list")
        if limit_per_group:
            records = records[:limit_per_group]
        groups[f"{method}/{domain}"] = records
    return groups


def run_seed_outputs(args: argparse.Namespace, cache: "JsonlOutputCache", groups: dict[str, list[dict[str, Any]]]) -> None:
    tasks_by_key: dict[str, dict[str, str]] = {}
    for records in groups.values():
        for record in records:
            prompt = normalize_text(record.get("original"))
            if not prompt:
                continue
            for run_label in ("seed_run_1", "seed_run_2"):
                key = target_cache_key(args, prompt, run_label)
                if cache.success(key):
                    continue
                tasks_by_key[key] = {"cache_key": key, "prompt": prompt, "run_label": run_label}

    tasks = list(tasks_by_key.values())
    print(f"[seed-output] missing={len(tasks)} existing={cache.success_count()}", flush=True)
    if not tasks:
        return

    completed = 0
    lock = threading.Lock()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_to_task = {
            executor.submit(fetch_target_output, args, task["prompt"], task["run_label"], task["cache_key"]): task
            for task in tasks
        }
        for future in concurrent.futures.as_completed(future_to_task):
            task = future_to_task[future]
            try:
                record = future.result()
            except Exception as exc:
                record = failure_cache_record(args, task["cache_key"], task["prompt"], task["run_label"], exc)
            with lock:
                cache.append(task["cache_key"], record)
                completed += 1
                if completed == 1 or completed % args.save_every == 0 or completed == len(tasks):
                    print(
                        f"[seed-output] completed={completed}/{len(tasks)} "
                        f"status={record['status']} run={task['run_label']}",
                        flush=True,
                    )


def fetch_target_output(
    args: argparse.Namespace,
    prompt: str,
    run_label: str,
    cache_key: str,
) -> dict[str, Any]:
    payload = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": TARGET_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
    }
    last_error: Exception | None = None
    for attempt in range(1, max(1, args.retries) + 1):
        try:
            data = post_json(chat_completions_url(args.base_url), payload, args.timeout)
            content = data["choices"][0]["message"].get("content") or ""
            return {
                "cache_key": cache_key,
                "status": "success",
                "model": args.model,
                "temperature": args.temperature,
                "max_tokens": args.max_tokens,
                "run_label": run_label,
                "prompt_sha256": sha256_text(prompt),
                "output": normalize_text(content),
                "error": "",
                "created_at": utc_now(),
                "attempt": attempt,
            }
        except Exception as exc:
            last_error = exc
            time.sleep(min(30, attempt))
    return failure_cache_record(args, cache_key, prompt, run_label, last_error)


def failure_cache_record(
    args: argparse.Namespace,
    cache_key: str,
    prompt: str,
    run_label: str,
    exc: Exception | None,
) -> dict[str, Any]:
    return {
        "cache_key": cache_key,
        "status": "failure",
        "model": args.model,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "run_label": run_label,
        "prompt_sha256": sha256_text(prompt),
        "output": "",
        "error": str(exc),
        "created_at": utc_now(),
    }


def compute_metrics(
    args: argparse.Namespace,
    cache: "JsonlOutputCache",
    groups: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "generated_at": utc_now(),
        "target_model": args.model,
        "target_temperature": args.temperature,
        "target_max_tokens": args.max_tokens,
        "sigma_mode": "seed_run_1 vs seed_run_2, two separate target-model calls per unique seed prompt",
        "overlap_metric": overlap_metric_description(),
        "groups": {},
    }

    for group_key, records in groups.items():
        method, domain = group_key.split("/", 1)
        group_metrics: list[dict[str, Any]] = []
        for record in records:
            prompt = normalize_text(record.get("original"))
            key_1 = target_cache_key(args, prompt, "seed_run_1")
            key_2 = target_cache_key(args, prompt, "seed_run_2")
            cached_1 = cache.get(key_1)
            cached_2 = cache.get(key_2)
            ok = bool(cached_1 and cached_1.get("status") == "success" and cached_2 and cached_2.get("status") == "success")
            output_1 = normalize_text(cached_1.get("output") if cached_1 else "")
            output_2 = normalize_text(cached_2.get("output") if cached_2 else "")
            sigma = char_overlap(output_1, output_2) if ok else None
            metric = {
                "method": method,
                "domain": domain,
                "language": record.get("language"),
                "country": record.get("country"),
                "source_key": record.get("source_key"),
                "source_id": record.get("source_id"),
                "task_type": record.get("task_type"),
                "seed_generated_char_overlap": record.get("seed_generated_char_overlap"),
                "seed_run_1_cache_key": key_1,
                "seed_run_2_cache_key": key_2,
                "sigma_seed_run_1_vs_seed_run_2": sigma,
                "status": "success" if ok else "failure",
                "error": "" if ok else first_error(cached_1, cached_2),
            }
            group_metrics.append(metric)
            metrics.append(metric)
        summary["groups"][group_key] = summarize_group(group_metrics)
    return metrics, summary


def first_error(*records: dict[str, Any] | None) -> str:
    for record in records:
        if record and record.get("error"):
            return str(record["error"])
    return "missing seed output"


def summarize_group(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    successes = [item for item in metrics if item.get("status") == "success"]
    sigma_values = [float(item["sigma_seed_run_1_vs_seed_run_2"]) for item in successes]
    return {
        "total": len(metrics),
        "success": len(successes),
        "failure": len(metrics) - len(successes),
        "avg_sigma": mean(sigma_values),
        "by_country": {
            country: summarize_shallow([item for item in successes if item.get("country") == country])
            for country in sorted({normalize_text(item.get("country")) for item in metrics if item.get("country")})
        },
    }


def summarize_shallow(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    sigma_values = [float(item["sigma_seed_run_1_vs_seed_run_2"]) for item in metrics]
    return {"count": len(metrics), "avg_sigma": mean(sigma_values)}


def write_markdown_summary(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Seed Sigma Evaluation",
        "",
        f"- Generated at: `{summary['generated_at']}`",
        f"- Target model: `{summary['target_model']}`",
        f"- Target temperature: `{summary['target_temperature']}`",
        f"- Target max tokens: `{summary['target_max_tokens']}`",
        f"- Sigma mode: {summary['sigma_mode']}",
        f"- Overlap metric: {summary['overlap_metric']}",
        "",
        "| Method | Domain | Count | Success | avg sigma |",
        "|---|---|---:|---:|---:|",
    ]
    for group_key, item in summary["groups"].items():
        method, domain = group_key.split("/", 1)
        lines.append(
            f"| {method} | {domain} | {item['total']} | {item['success']} | "
            f"{format_optional_float(item['avg_sigma'])} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


class JsonlOutputCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.records: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = normalize_text(item.get("cache_key"))
                if key:
                    self.records[key] = item

    def get(self, key: str) -> dict[str, Any] | None:
        return self.records.get(key)

    def success(self, key: str) -> bool:
        record = self.records.get(key)
        return bool(record and record.get("status") == "success")

    def success_count(self) -> int:
        return sum(1 for item in self.records.values() if item.get("status") == "success")

    def append(self, key: str, record: dict[str, Any]) -> None:
        self.records[key] = record
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def chat_completions_url(base_url: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", "chat/completions")


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFKC", str(value)).strip().lower().split())


def char_overlap(left: Any, right: Any) -> float:
    left_text = normalize_text(left)
    right_text = normalize_text(right)
    if not left_text and not right_text:
        return 1.0
    if not left_text or not right_text:
        return 0.0
    left_counts = Counter(left_text)
    right_counts = Counter(right_text)
    overlap = sum(min(left_counts[ch], right_counts[ch]) for ch in left_counts.keys() | right_counts.keys())
    return 2 * overlap / (len(left_text) + len(right_text))


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def target_cache_key(args: argparse.Namespace, prompt: str, run_label: str) -> str:
    payload = {
        "model": args.model,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "run_label": run_label,
        "prompt": prompt,
    }
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def overlap_metric_description() -> str:
    return (
        "Unicode NFKC + lowercase + whitespace collapse, then character-frequency "
        "Sorensen-Dice overlap: 2*sum(min(countA,countB))/(len(A)+len(B))."
    )


def format_optional_float(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.6f}"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
