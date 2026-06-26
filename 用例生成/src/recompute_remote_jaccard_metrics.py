#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


ROOT = Path("result_true_sigma_seed_only")
DATA = Path("data")
MODEL = "Qwen2.5-7B"
TEMP = 0.0
MAX_TOKENS = 128

GROUP_FILES = [
    ("dialect", "content_safety", "filtered/dialect/content_safety_dialect_kept.json"),
    ("dialect", "privacy", "filtered/dialect/privacy_dialect_kept.json"),
    ("qwen_rewrite", "content_safety", "filtered/qwen_rewrite/content_safety_qwen_rewrite_kept.json"),
    ("qwen_rewrite", "privacy", "filtered/qwen_rewrite/privacy_qwen_rewrite_kept.json"),
]


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFKC", str(value)).strip().lower().split())


def normalize_chars(value: Any) -> set[str]:
    return set(normalize_text(value))


def char_jaccard(left: str, right: str) -> Optional[float]:
    """Return character-set Jaccard similarity in [0, 1]."""
    left_chars = normalize_chars(left)
    right_chars = normalize_chars(right)

    if not left_chars and not right_chars:
        return None
    if not left_chars or not right_chars:
        return 0.0

    return len(left_chars & right_chars) / len(left_chars | right_chars)


def load_cache(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            key = item.get("cache_key")
            if key:
                records[key] = item
    return records


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def target_cache_key(prompt: str, run_label: str) -> str:
    payload = {
        "model": MODEL,
        "temperature": TEMP,
        "max_tokens": MAX_TOKENS,
        "run_label": run_label,
        "prompt": prompt,
    }
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def mean(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def fmt(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.6f}"


def main() -> int:
    seed_cache = load_cache(ROOT / "seed_output_cache.jsonl")
    generated_cache = load_cache(ROOT / "generated_output_cache.jsonl")
    metrics: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_model": MODEL,
        "target_temperature": TEMP,
        "target_max_tokens": MAX_TOKENS,
        "sigma_mode": "1 - character-set Jaccard(seed_run_1_output, seed_run_2_output)",
        "x_mode": "1 - character-set Jaccard(seed_run_1_output, generated_run_1_output)",
        "normalization": "Unicode NFKC + lowercase + whitespace collapse, then set(text).",
        "groups": {},
    }

    for method, domain, relative_path in GROUP_FILES:
        records = json.loads((DATA / relative_path).read_text(encoding="utf-8"))
        group_metrics: list[dict[str, Any]] = []
        for record in records:
            seed_prompt = normalize_text(record.get("original"))
            generated_prompt = normalize_text(record.get("generated"))
            seed_key_1 = target_cache_key(seed_prompt, "seed_run_1")
            seed_key_2 = target_cache_key(seed_prompt, "seed_run_2")
            generated_key = target_cache_key(generated_prompt, "generated_run_1")
            seed_1 = seed_cache.get(seed_key_1)
            seed_2 = seed_cache.get(seed_key_2)
            generated = generated_cache.get(generated_key)
            ok = all(
                item and item.get("status") == "success"
                for item in (seed_1, seed_2, generated)
            )
            sigma = None
            x_value = None
            if ok:
                sigma_similarity = char_jaccard(seed_1.get("output", ""), seed_2.get("output", ""))
                x_similarity = char_jaccard(seed_1.get("output", ""), generated.get("output", ""))
                if sigma_similarity is not None and x_similarity is not None:
                    sigma = 1 - sigma_similarity
                    x_value = 1 - x_similarity
            metric = {
                "method": method,
                "domain": domain,
                "source_key": record.get("source_key"),
                "status": "success" if sigma is not None and x_value is not None else "failure",
                "avg_sigma_component": sigma,
                "x_seed_vs_generated": x_value,
            }
            group_metrics.append(metric)
            metrics.append(metric)

        successes = [item for item in group_metrics if item["status"] == "success"]
        sigma_values = [float(item["avg_sigma_component"]) for item in successes]
        x_values = [float(item["x_seed_vs_generated"]) for item in successes]
        avg_sigma = mean(sigma_values)
        avg_x = mean(x_values)
        three_avg_sigma = None if avg_sigma is None else 3 * avg_sigma
        summary["groups"][f"{method}/{domain}"] = {
            "total": len(group_metrics),
            "success": len(successes),
            "failure": len(group_metrics) - len(successes),
            "avg_sigma": avg_sigma,
            "avg_x": avg_x,
            "three_avg_sigma": three_avg_sigma,
            "avg_x_greater_than_3avg_sigma": None
            if avg_x is None or three_avg_sigma is None
            else avg_x > three_avg_sigma,
        }

    (ROOT / "x_pair_metrics_jaccard.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (ROOT / "x_summary_jaccard.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# Jaccard Seed Sigma and X Evaluation",
        "",
        f"- Generated at: `{summary['generated_at']}`",
        f"- Target model: `{MODEL}`",
        f"- Target temperature: `{TEMP}`",
        f"- Target max tokens: `{MAX_TOKENS}`",
        f"- Sigma mode: {summary['sigma_mode']}",
        f"- X mode: {summary['x_mode']}",
        f"- Normalization: {summary['normalization']}",
        "",
        "| Method | Domain | Count | Success | avg sigma | avg x | 3 avg sigma | x > 3 avg sigma |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for group_key, group in summary["groups"].items():
        method, domain = group_key.split("/", 1)
        lines.append(
            f"| {method} | {domain} | {group['total']} | {group['success']} | "
            f"{fmt(group['avg_sigma'])} | {fmt(group['avg_x'])} | "
            f"{fmt(group['three_avg_sigma'])} | {group['avg_x_greater_than_3avg_sigma']} |"
        )
    lines.append("")
    markdown = "\n".join(lines)
    (ROOT / "x_summary_jaccard.md").write_text(markdown + "\n", encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
