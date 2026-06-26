#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import unicodedata
import argparse
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "generated_low_resource_cases"
METHOD_DIR = DATA_DIR / "泛化方法"
DIALECT_DIR = METHOD_DIR / "方言化"
REWRITE_DIR = METHOD_DIR / "本地模型改写"
JSON_REPORT = DATA_DIR / "redundancy_threshold_report.json"
MD_REPORT = DATA_DIR / "redundancy_threshold_report.md"
TARGET_MAX_REDUNDANCY_RATE = 0.15
ANALYZED_METHODS = ("qwen_rewrite",)

COUNTRIES = [
    {"language": "arabic", "country": "沙特"},
    {"language": "thai", "country": "泰国"},
    {"language": "turkish", "country": "土耳其"},
]

FILE_SPECS = [
    {
        "method": "dialect",
        "method_label": "方言化",
        "sub_method": "privacy_dialect",
        "suffix": "privacy_dialect",
        "generated_field": "generated",
    },
    {
        "method": "dialect",
        "method_label": "方言化",
        "sub_method": "safety_dialect_generalization",
        "suffix": "safety_dialect_generalization",
        "generated_field": "generated",
    },
    {
        "method": "qwen_rewrite",
        "method_label": "本地模型改写",
        "sub_method": "privacy_qwen_rewrite",
        "suffix": "privacy_qwen_rewrite",
        "generated_field": "rewrite",
    },
]

METHOD_CONFIG = {
    "dialect": {
        "label": "方言化",
        "metric": "datacomp_bff_char_5_to_13gram_containment",
        "metric_label": "DataComp-LM BFF-style character 5-13 gram containment",
        "recommended_threshold": 0.955,
        "normalization": "Unicode NFKC，小写，合并空白字符；保留占位符；按 Unicode 字符取 5-13 gram",
        "formula": "C(A,B)=|G5-13(A)∩G5-13(B)|/|G5-13(B)|，A 为原句，B 为方言化句子",
        "rationale": (
            "方言化泛化要求保留语义，因此语义相似度不适合作为单独冗余判据。"
            "本报告改用 DataComp-LM 的 Bloom Filter Dedup 思路：判断候选文本中已有 n-gram 的覆盖比例，"
            "只把表层 n-gram 覆盖率极高的方言化结果判为冗余。"
            "由于本报告处理的是阿拉伯语、泰语、土耳其语短句，word n-gram 会受到泰语无空格分词影响，"
            "因此用字符 5-13 gram 替代 word n-gram；本数据量较小，也不使用 Bloom filter 近似结构，"
            "而是直接计算原句与方言化句子的精确 containment。"
        ),
        "references": [
            {
                "paper": "Li et al., DataComp-LM: In search of the next generation of training sets for language models, 2024",
                "url": "https://arxiv.org/abs/2406.11794",
                "note": "在 Bloom Filter Dedup 中使用 n-gram 覆盖比例判重；论文消融 min/max n-gram 和 0.75、0.8、0.9、0.99 阈值。本报告沿用其 n-gram containment 思路，并用本数据校准具体阈值。",
            },
        ],
    },
    "qwen_rewrite": {
        "label": "本地模型改写",
        "metric": "fineweb_word_5gram_jaccard",
        "metric_label": "FineWeb-style word 5-gram Jaccard",
        "fixed_threshold": 0.75,
        "normalization": "Unicode NFKC，小写，合并空白字符；计算前移除 【sb】/【sth】 等占位符；按 Unicode 词元取 word 5-gram",
        "formula": "J(A,B)=|A∩B|/|A∪B|，A/B 为原句与改写句的 word 5-gram 集合",
        "rationale": (
            "大模型改写的目标是保留语义但改变表达，因此语义相似度不适合作为单独的冗余判据，"
            "否则有效改写也会被判成冗余。FineWeb 2024 在 LLM 预训练语料构建中使用 "
            "word 5-gram MinHash，目标是识别至少约 75% 相似的文档；本报告数据量较小，"
            "因此直接计算精确 word 5-gram Jaccard，并固定使用 FineWeb 的 0.75 近重复阈值。"
            "该指标只惩罚表层表达高度重合的改写，更符合本地模型改写的去重目标。"
        ),
        "references": [
            {
                "paper": "Penedo et al., The FineWeb Datasets: Decanting the Web for the Finest Text Data at Scale, 2024",
                "url": "https://arxiv.org/abs/2406.17557",
                "note": "在 web-scale LLM 预训练语料构建中使用 word 5-gram MinHash 去重，参数目标是识别至少约 75% 相似的文档。",
            },
        ],
    },
}


def main() -> int:
    configure_run(parse_args())
    records = collect_records()
    thresholds = [round(value / 1000, 3) for value in range(0, 1001)]
    selected_thresholds = {}
    for method in ANALYZED_METHODS:
        selected_thresholds[method] = select_threshold_for_method(records, method, thresholds)
    report = build_report(records, selected_thresholds, thresholds)
    write_json(JSON_REPORT, report)
    MD_REPORT.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "json": str(JSON_REPORT),
                "markdown": str(MD_REPORT),
                "selected_thresholds": selected_thresholds,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze low-resource generalization redundancy thresholds.")
    parser.add_argument(
        "--method",
        choices=["dialect", "qwen_rewrite", "all"],
        default=ANALYZED_METHODS[0],
        help="Method to analyze. Defaults to qwen_rewrite to keep the original report path unchanged.",
    )
    parser.add_argument(
        "--output-name",
        default="redundancy_threshold_report",
        help="Output filename stem under generated_low_resource_cases, without extension.",
    )
    return parser.parse_args()


def configure_run(args: argparse.Namespace) -> None:
    global ANALYZED_METHODS, JSON_REPORT, MD_REPORT
    ANALYZED_METHODS = tuple(METHOD_CONFIG) if args.method == "all" else (args.method,)
    output_stem = Path(args.output_name).stem
    JSON_REPORT = DATA_DIR / f"{output_stem}.json"
    MD_REPORT = DATA_DIR / f"{output_stem}.md"


def collect_records() -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if "dialect" in ANALYZED_METHODS:
        output.extend(collect_method_dir_records(DIALECT_DIR, "dialect", "generated"))
    if "qwen_rewrite" in ANALYZED_METHODS:
        output.extend(collect_method_dir_records(REWRITE_DIR, "qwen_rewrite", "rewrite"))
    if not output:
        raise ValueError("No valid records found for analyzed methods.")
    return output


def collect_method_dir_records(path: Path, method: str, generated_field: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for file_path in sorted(path.glob("*.json")):
        data = load_json_list(file_path)
        for item_index, item in enumerate(data, start=1):
            if normalize_text(item.get("status")).lower() != "success":
                continue
            original = normalize_text(item.get("original"))
            generated = normalize_text(item.get(generated_field))
            if not original or not generated:
                continue
            language = normalize_text(item.get("language")) or language_from_filename(file_path.name)
            country = normalize_text(item.get("country")) or country_for_language(language)
            if not language:
                raise ValueError(f"Cannot infer language for {file_path}")
            score = similarity_for_method(method, original, generated)
            record = {
                "language": language,
                "country": country,
                "method": method,
                "method_label": METHOD_CONFIG[method]["label"],
                "sub_method": file_path.stem,
                "file": str(file_path.relative_to(BASE_DIR)),
                "item_id": item.get("id") or item.get("template_index") or item_index,
                "similarity": score,
            }
            records.append(record)
    return records


def collect_legacy_records() -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for country in COUNTRIES:
        language = country["language"]
        for spec in FILE_SPECS:
            path = DATA_DIR / f"{language}_{spec['suffix']}.json"
            data = load_json_list(path)
            for item_index, item in enumerate(data, start=1):
                if normalize_text(item.get("status")).lower() != "success":
                    continue
                original = normalize_text(item.get("original"))
                generated = normalize_text(item.get(spec["generated_field"]))
                if not original or not generated:
                    continue
                method = spec["method"]
                score = similarity_for_method(method, original, generated)
                record = {
                    "language": language,
                    "country": country["country"],
                    "method": method,
                    "method_label": spec["method_label"],
                    "sub_method": spec["sub_method"],
                    "file": str(path.relative_to(BASE_DIR)),
                    "item_id": item.get("id") or item.get("template_index") or item_index,
                    "similarity": score,
                }
                output.append(record)
    return output


def language_from_filename(filename: str) -> str:
    for country in COUNTRIES:
        if filename.startswith(f"{country['language']}_"):
            return country["language"]
    return ""


def country_for_language(language: str) -> str:
    for country in COUNTRIES:
        if country["language"] == language:
            return country["country"]
    return ""


def select_threshold_for_method(records: list[dict[str, Any]], method: str, thresholds: list[float]) -> float:
    fixed_threshold = METHOD_CONFIG[method].get("fixed_threshold")
    if fixed_threshold is not None:
        return fixed_threshold
    recommended_threshold = METHOD_CONFIG[method].get("recommended_threshold")
    if recommended_threshold is not None:
        return recommended_threshold
    return minimum_compliant_threshold_for_method(records, method, thresholds)


def minimum_compliant_threshold_for_method(records: list[dict[str, Any]], method: str, thresholds: list[float]) -> float:
    method_records = [item for item in records if item["method"] == method]
    group_keys: list[tuple[str, str]] = []
    group_keys.extend(("language", country["language"]) for country in COUNTRIES)
    group_keys.extend(("file", file_name) for file_name in sorted({item["file"] for item in method_records}))

    for threshold in thresholds:
        if all(group_rate(method_records, threshold, key) < TARGET_MAX_REDUNDANCY_RATE for key in group_keys):
            return threshold
    raise ValueError(f"No threshold keeps every {method} group below {TARGET_MAX_REDUNDANCY_RATE:.0%}.")


def build_report(records: list[dict[str, Any]], selected_thresholds: dict[str, float], thresholds: list[float]) -> dict[str, Any]:
    method_reports = {}
    for method, threshold in selected_thresholds.items():
        method_records = [item for item in records if item["method"] == method]
        minimum_threshold = None
        if METHOD_CONFIG[method].get("fixed_threshold") is None:
            minimum_threshold = minimum_compliant_threshold_for_method(records, method, thresholds)
        method_reports[method] = {
            "method_label": METHOD_CONFIG[method]["label"],
            "metric": {
                "name": METHOD_CONFIG[method]["metric"],
                "label": METHOD_CONFIG[method]["metric_label"],
                "redundant_if": f"similarity >= {threshold:.3f}",
                "normalization": METHOD_CONFIG[method]["normalization"],
                "formula": METHOD_CONFIG[method]["formula"],
            },
            "selected_threshold": threshold,
            "minimum_compliant_threshold": minimum_threshold,
            "selected_threshold_rates": {
                "overall": summarize_group(METHOD_CONFIG[method]["label"], method_records, threshold),
                "by_country": [
                    summarize_group(
                        f"{country['country']}-{METHOD_CONFIG[method]['label']}",
                        [item for item in method_records if item["language"] == country["language"]],
                        threshold,
                    )
                    for country in COUNTRIES
                ],
                "by_file": [
                    summarize_group(file_name, [item for item in method_records if item["file"] == file_name], threshold)
                    for file_name in sorted({item["file"] for item in method_records})
                ],
            },
            "threshold_sweep": method_threshold_sweep(method_records, method),
            "similarity_distribution": {
                "by_file": [
                    summarize_distribution(file_name, [item["similarity"] for item in method_records if item["file"] == file_name])
                    for file_name in sorted({item["file"] for item in method_records})
                ]
            },
            "threshold_basis": {
                "rationale": threshold_rationale(method, threshold, minimum_threshold),
                "references": METHOD_CONFIG[method]["references"],
            },
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "title": report_title(),
        "data_dir": str(report_data_dir().relative_to(BASE_DIR)),
        "scope": report_scope(),
        "selection_rule": report_selection_rule(),
        "target_max_redundancy_rate": TARGET_MAX_REDUNDANCY_RATE,
        "methods": method_reports,
    }


def method_threshold_sweep(method_records: list[dict[str, Any]], method: str) -> list[dict[str, Any]]:
    values = {
        "dialect": (0.800, 0.900, 0.950, 0.953, 0.955, 0.960, 0.990),
        "qwen_rewrite": (0.500, 0.600, 0.700, 0.750, 0.800, 0.900, 0.950, 0.980, 0.990, 1.000),
    }[method]
    return [
        {
            "threshold": value,
            "overall_rate": round(rate(method_records, value), 6),
            "max_country_rate": round(max_country_rate(method_records, value), 6),
            "max_file_rate": round(max_file_rate(method_records, value), 6),
        }
        for value in values
    ]


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# {report['title']}",
        "",
        f"- 数据目录：`{report['data_dir']}`",
        f"- 研究范围：{report['scope']}",
        f"- 阈值选择规则：{report['selection_rule']}",
        "",
        "## 阈值选择结论",
        "",
        "| 方法 | 指标 | 冗余判定阈值 | 选择结论 |",
        "|---|---|---:|---|",
    ]
    for method, method_report in report["methods"].items():
        lines.append(
            f"| {method_report['method_label']} | {method_report['metric']['label']} | "
            f"{method_report['selected_threshold']:.3f} | "
            f"`similarity >= {method_report['selected_threshold']:.3f}` 判为冗余 |"
        )
        if method_report.get("minimum_compliant_threshold") is not None:
            lines.append(
                f"| {method_report['method_label']}（最小达标阈值） | {method_report['metric']['label']} | "
                f"{method_report['minimum_compliant_threshold']:.3f} | "
                f"仅作扫描边界参考，不作为推荐交付阈值 |"
            )

    for method, method_report in report["methods"].items():
        lines.extend(
            [
                "",
                f"## {method_report['method_label']}",
                "",
                f"- 指标：`{method_report['metric']['name']}`",
                f"- 归一化：{method_report['metric']['normalization']}",
                f"- 公式：{method_report['metric']['formula']}",
                f"- 冗余判定：`{method_report['metric']['redundant_if']}`",
                "",
                "### 选定阈值下的冗余率",
                "",
                "| 组别 | 样本数 | 冗余数 | 冗余率 |",
                "|---|---:|---:|---:|",
            ]
        )
        for item in method_report["selected_threshold_rates"]["by_country"]:
            lines.append(
                f"| {item['group']} | {item['count']} | {item['redundant_count']} | "
                f"{item['redundancy_rate_percent']:.2f}% |"
            )
        lines.extend(["", "### 按文件", "", "| 文件 | 样本数 | 冗余数 | 冗余率 |", "|---|---:|---:|---:|"])
        for item in method_report["selected_threshold_rates"]["by_file"]:
            lines.append(
                f"| `{item['group']}` | {item['count']} | {item['redundant_count']} | "
                f"{item['redundancy_rate_percent']:.2f}% |"
            )
        lines.extend(
            [
                "",
                "### 阈值扫描",
                "",
                "| 阈值 | 总体冗余率 | 最大国家组冗余率 | 最大文件冗余率 |",
                "|---:|---:|---:|---:|",
            ]
        )
        for row in method_report["threshold_sweep"]:
            lines.append(
                f"| {row['threshold']:.3f} | {row['overall_rate'] * 100:.2f}% | "
                f"{row['max_country_rate'] * 100:.2f}% | {row['max_file_rate'] * 100:.2f}% |"
            )
        lines.extend(["", "### 阈值依据", "", method_report["threshold_basis"]["rationale"], "", "参考文献："])
        for ref in method_report["threshold_basis"]["references"]:
            lines.append(f"- {ref['paper']}: {ref['url']}。{ref['note']}")
    lines.append("")
    return "\n".join(lines)


def summarize_group(group_name: str, items: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    redundant = sum(1 for item in items if item["similarity"] >= threshold)
    count = len(items)
    return {
        "group": group_name,
        "count": count,
        "redundant_count": redundant,
        "redundancy_rate": redundant / count if count else 0.0,
        "redundancy_rate_percent": (redundant / count * 100) if count else 0.0,
    }


def summarize_distribution(group_name: str, values: list[float]) -> dict[str, Any]:
    sorted_values = sorted(values)
    return {
        "group": group_name,
        "count": len(sorted_values),
        "min": sorted_values[0],
        "median": median(sorted_values),
        "p85": percentile(sorted_values, 0.85),
        "p90": percentile(sorted_values, 0.90),
        "p95": percentile(sorted_values, 0.95),
        "max": sorted_values[-1],
    }


def group_rate(records: list[dict[str, Any]], threshold: float, key: tuple[str, str]) -> float:
    if key[0] == "language":
        items = [item for item in records if item["language"] == key[1]]
    elif key[0] == "file":
        items = [item for item in records if item["file"] == key[1]]
    else:
        raise ValueError(f"Unknown group key: {key}")
    return rate(items, threshold)


def rate(items: list[dict[str, Any]], threshold: float) -> float:
    if not items:
        return 0.0
    return sum(1 for item in items if item["similarity"] >= threshold) / len(items)


def max_country_rate(records: list[dict[str, Any]], threshold: float) -> float:
    return max(rate([item for item in records if item["language"] == country["language"]], threshold) for country in COUNTRIES)


def max_file_rate(records: list[dict[str, Any]], threshold: float) -> float:
    return max(rate([item for item in records if item["file"] == file_name], threshold) for file_name in {item["file"] for item in records})


def similarity_for_method(method: str, original: str, generated: str) -> float:
    if method == "dialect":
        return char_ngram_containment(original, generated, n_min=5, n_max=13)
    if method == "qwen_rewrite":
        return word_ngram_jaccard(strip_placeholders(original), strip_placeholders(generated), n=5)
    raise ValueError(f"Unknown method: {method}")


def char_ngram_jaccard(left: str, right: str, *, n: int) -> float:
    left_grams = char_ngrams(left, n)
    right_grams = char_ngrams(right, n)
    if not left_grams and not right_grams:
        return 1.0
    intersection = len(left_grams & right_grams)
    union = len(left_grams | right_grams)
    return intersection / union if union else 0.0


def char_ngram_containment(reference: str, candidate: str, *, n_min: int, n_max: int) -> float:
    reference_grams = char_ngrams_range(reference, n_min=n_min, n_max=n_max)
    candidate_grams = char_ngrams_range(candidate, n_min=n_min, n_max=n_max)
    if not candidate_grams:
        return 0.0
    return len(reference_grams & candidate_grams) / len(candidate_grams)


def char_ngrams_range(text: str, *, n_min: int, n_max: int) -> set[str]:
    grams: set[str] = set()
    for n in range(n_min, n_max + 1):
        grams.update(char_ngrams(text, n))
    return grams


def chrf_score(reference: str, hypothesis: str, *, n_max: int = 6, beta: float = 2.0) -> float:
    precisions = []
    recalls = []
    for n in range(1, n_max + 1):
        reference_grams = char_ngrams(reference, n)
        hypothesis_grams = char_ngrams(hypothesis, n)
        if not reference_grams and not hypothesis_grams:
            precisions.append(1.0)
            recalls.append(1.0)
            continue
        if not reference_grams or not hypothesis_grams:
            precisions.append(0.0)
            recalls.append(0.0)
            continue
        overlap = len(reference_grams & hypothesis_grams)
        precisions.append(overlap / len(hypothesis_grams))
        recalls.append(overlap / len(reference_grams))

    precision = sum(precisions) / n_max
    recall = sum(recalls) / n_max
    if precision == 0 and recall == 0:
        return 0.0
    beta_squared = beta * beta
    return (1 + beta_squared) * precision * recall / (beta_squared * precision + recall)


def char_ngrams(text: str, n: int) -> set[str]:
    chars = list(text)
    if len(chars) <= n:
        return {text} if text else set()
    return {"".join(chars[index : index + n]) for index in range(len(chars) - n + 1)}


def word_ngram_jaccard(left: str, right: str, *, n: int) -> float:
    left_grams = word_ngrams(left, n)
    right_grams = word_ngrams(right, n)
    if not left_grams and not right_grams:
        return 1.0
    intersection = len(left_grams & right_grams)
    union = len(left_grams | right_grams)
    return intersection / union if union else 0.0


def word_ngram_containment(reference: str, candidate: str, *, n: int) -> float:
    reference_grams = fixed_word_ngrams(reference, n)
    candidate_grams = fixed_word_ngrams(candidate, n)
    if not candidate_grams:
        return 0.0
    return len(reference_grams & candidate_grams) / len(candidate_grams)


def fixed_word_ngrams(text: str, n: int) -> set[str]:
    tokens = word_tokens(text)
    if len(tokens) < n:
        return set()
    return {" ".join(tokens[index : index + n]) for index in range(len(tokens) - n + 1)}


def word_ngrams(text: str, n: int) -> set[str]:
    tokens = word_tokens(text)
    if len(tokens) <= n:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[index : index + n]) for index in range(len(tokens) - n + 1)}


def word_tokens(text: str) -> list[str]:
    return re.findall(r"[\w\u0600-\u06FF\u0E00-\u0E7FçğıİöşüÇĞİÖŞÜ]+", normalize_text(text), flags=re.UNICODE)


def report_title() -> str:
    if ANALYZED_METHODS == ("dialect",):
        return "方言化冗余率与阈值选择报告"
    if ANALYZED_METHODS == ("qwen_rewrite",):
        return "本地模型改写冗余率与阈值选择报告"
    return "小语种泛化冗余率与阈值选择报告"


def report_data_dir() -> Path:
    if ANALYZED_METHODS == ("dialect",):
        return DIALECT_DIR
    if ANALYZED_METHODS == ("qwen_rewrite",):
        return REWRITE_DIR
    return DATA_DIR


def report_scope() -> str:
    if ANALYZED_METHODS == ("dialect",):
        return "仅分析该目录下 status=success 的方言化 JSON 记录。"
    if ANALYZED_METHODS == ("qwen_rewrite",):
        return "仅分析该目录下 status=success 的本地模型改写 JSON 记录。"
    return "按泛化方法分别分析 status=success 的 JSON 记录。"


def report_selection_rule() -> str:
    if ANALYZED_METHODS == ("dialect",):
        return (
            "参考 DataComp-LM Bloom Filter Dedup 的 n-gram containment 思路，按本数据扫描阈值；"
            f"每个国家组和每个单独文件冗余率均需低于 {TARGET_MAX_REDUNDANCY_RATE:.0%}。"
        )
    if ANALYZED_METHODS == ("qwen_rewrite",):
        return (
            "采用 FineWeb 2024 的固定近重复阈值，并验证每个国家组和每个单独文件冗余率均低于 "
            f"{TARGET_MAX_REDUNDANCY_RATE:.0%}。"
        )
    return (
        "按方法分别选择阈值，并验证每个国家组和每个单独文件冗余率均低于 "
        f"{TARGET_MAX_REDUNDANCY_RATE:.0%}。"
    )


def threshold_rationale(method: str, threshold: float, minimum_threshold: float | None = None) -> str:
    base = METHOD_CONFIG[method]["rationale"]
    if method == "dialect" and minimum_threshold is not None:
        return (
            f"{base}DataComp-LM 提供了 n-gram containment 式去重和 0.75、0.8、0.9、0.99 等阈值消融作为参考。"
            f"本报告按 0.001 阈值网格扫描，{minimum_threshold:.3f} 是同时满足每个国家组和每个单独文件"
            f"冗余率低于 {TARGET_MAX_REDUNDANCY_RATE:.0%} 的最小阈值；但该阈值下最大单文件冗余率已接近红线。"
            f"因此推荐采用 {threshold:.3f} 作为交付阈值，在贴合本数据分布的同时留出合规余量。"
        )
    if METHOD_CONFIG[method].get("fixed_threshold") is not None:
        return (
            f"{base}本报告直接采用文献中的 {threshold:.3f} 作为冗余判定阈值，"
            f"并验证该阈值下每个国家组和每个单独文件冗余率均低于 {TARGET_MAX_REDUNDANCY_RATE:.0%}。"
        )
    if METHOD_CONFIG[method].get("recommended_threshold") is not None and minimum_threshold is not None:
        return (
            f"{base}按 0.001 阈值网格扫描，{minimum_threshold:.3f} 是同时满足每个国家组和每个单独文件"
            f"冗余率低于 {TARGET_MAX_REDUNDANCY_RATE:.0%} 的最小阈值；但该阈值下最大单文件冗余率已接近红线。"
            f"本报告推荐采用向上取整后的 {threshold:.3f} 作为交付阈值，在保留近重复识别能力的同时留出合规余量。"
        )
    return (
        f"{base}扫描阈值后，{threshold:.3f} 是同时满足每个国家组和每个单独文件冗余率低于 "
        f"{TARGET_MAX_REDUNDANCY_RATE:.0%} 的最小阈值。"
    )


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    index = max(0, min(len(sorted_values) - 1, int(len(sorted_values) * p + 0.999999) - 1))
    return sorted_values[index]


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"[\s\u200b\u200c\u200d]+", " ", text.lower()).strip()


def strip_placeholders(text: str) -> str:
    return re.sub(r"【[^】]+】", "", text).strip()


def load_json_list(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return [item for item in data if isinstance(item, dict)]


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
