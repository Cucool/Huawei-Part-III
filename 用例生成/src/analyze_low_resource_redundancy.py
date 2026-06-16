#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "generated_low_resource_cases"
JSON_REPORT = DATA_DIR / "redundancy_threshold_report.json"
MD_REPORT = DATA_DIR / "redundancy_threshold_report.md"
TARGET_MAX_REDUNDANCY_RATE = 0.15

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
        "metric": "character_3gram_jaccard",
        "metric_label": "字符 3-gram Jaccard",
        "normalization": "Unicode NFKC，小写，合并空白字符；保留占位符",
        "formula": "J(A,B)=|A∩B|/|A∪B|，A/B 为字符 3-gram 集合",
        "rationale": (
            "方言化泛化主要通过少量字母、音系或词尾替换实现，冗余风险是只改动了极少字符。"
            "因此用字符 3-gram Jaccard 直接衡量原句与方言化句子的表层重叠度；"
            "分词对阿拉伯语、泰语、土耳其语并不一致，字符级 n-gram 更稳。"
        ),
        "references": [
            {
                "paper": "Broder, On the Resemblance and Containment of Documents, 1997",
                "url": "https://doi.org/10.1109/SEQUEN.1997.666900",
                "note": "提出用 shingled sets/Jaccard resemblance 做文档近重复检测，是本类表层近重复判定的基础。",
            },
            {
                "paper": "Lee et al., Deduplicating Training Data Makes Language Models Better, ACL 2022",
                "url": "https://arxiv.org/abs/2107.06499",
                "note": "在语言模型训练数据去重中使用精确匹配和 MinHash 近似匹配，近重复以高 n-gram/Jaccard 重叠为核心信号。",
            },
        ],
    },
    "qwen_rewrite": {
        "label": "本地模型改写",
        "metric": "chrf2_char_1_to_6",
        "metric_label": "chrF2 字符 1-6 gram F-score",
        "normalization": "Unicode NFKC，小写，合并空白字符；计算前移除 【sb】/【sth】 等占位符",
        "formula": "chrFβ=(1+β²)PR/(β²P+R)，β=2，P/R 为字符 1-6 gram 平均精确率/召回率",
        "rationale": (
            "大模型改写的目标是语义保持但表达变化，因此不能把“语义相似”本身当作冗余。"
            "冗余应看改写句是否大量复用原句表述。chrF 用字符 n-gram 的精确率和召回率衡量表层重合，"
            "比单纯 Jaccard 更适合长短略有变化的生成式改写；移除占位符是为了避免固定槽位抬高相似度。"
        ),
        "references": [
            {
                "paper": "Popović, chrF: character n-gram F-score for automatic MT evaluation, WMT 2015",
                "url": "https://aclanthology.org/W15-3049/",
                "note": "提出 chrF，用字符 n-gram F-score 评价生成文本与参考文本的重合度，适合跨语言且不依赖分词。",
            },
            {
                "paper": "Papineni et al., BLEU: a Method for Automatic Evaluation of Machine Translation, ACL 2002",
                "url": "https://aclanthology.org/P02-1040/",
                "note": "BLEU 将 n-gram 重合用于自动评价生成文本，是用表层重合衡量生成输出接近程度的经典依据。",
            },
            {
                "paper": "Zhu et al., Texygen: A Benchmarking Platform for Text Generation Models, 2018",
                "url": "https://arxiv.org/abs/1802.01886",
                "note": "在文本生成评测中区分 quality、diversity、consistency，支持把生成多样性与内容一致性分开看待。",
            },
        ],
    },
}


def main() -> int:
    records = collect_records()
    thresholds = [round(value / 1000, 3) for value in range(0, 996)]
    selected_thresholds = {
        method: select_threshold_for_method(records, method, thresholds)
        for method in METHOD_CONFIG
    }
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


def collect_records() -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for country in COUNTRIES:
        language = country["language"]
        for spec in FILE_SPECS:
            path = DATA_DIR / f"{language}_{spec['suffix']}.json"
            data = load_json_list(path)
            for item_index, item in enumerate(data, start=1):
                if normalize_text(item.get("status")).lower() != "success":
                    continue
                original_raw = item.get("original")
                generated_raw = item.get(spec["generated_field"])
                original = normalize_text(original_raw)
                generated = normalize_text(generated_raw)
                if not original or not generated:
                    continue
                method = spec["method"]
                if method == "dialect":
                    score = char_ngram_jaccard(original, generated, n=3)
                elif method == "qwen_rewrite":
                    score = chrf_score(
                        strip_placeholders(original),
                        strip_placeholders(generated),
                        n_max=6,
                        beta=2.0,
                    )
                else:
                    raise ValueError(f"Unknown method: {method}")
                output.append(
                    {
                        "language": language,
                        "country": country["country"],
                        "method": method,
                        "method_label": spec["method_label"],
                        "sub_method": spec["sub_method"],
                        "file": str(path.relative_to(BASE_DIR)),
                        "item_id": item.get("id") or item.get("template_index") or item_index,
                        "similarity": score,
                    }
                )
    if not output:
        raise ValueError(f"No valid records found in {DATA_DIR}")
    return output


def select_threshold_for_method(records: list[dict[str, Any]], method: str, thresholds: list[float]) -> float:
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
                "rationale": (
                    f"{METHOD_CONFIG[method]['rationale']}"
                    f"扫描阈值后，{threshold:.3f} 是同时满足每个国家组和每个单独文件冗余率低于 "
                    f"{TARGET_MAX_REDUNDANCY_RATE:.0%} 的最小阈值。"
                ),
                "references": METHOD_CONFIG[method]["references"],
            },
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(DATA_DIR.relative_to(BASE_DIR)),
        "selection_rule": (
            "Thresholds are selected separately by method. For each method, choose the smallest scanned threshold "
            "such that redundancy is below 15% for every country aggregate and every individual source file."
        ),
        "target_max_redundancy_rate": TARGET_MAX_REDUNDANCY_RATE,
        "methods": method_reports,
    }


def method_threshold_sweep(method_records: list[dict[str, Any]], method: str) -> list[dict[str, Any]]:
    values = {
        "dialect": (0.950, 0.960, 0.970, 0.980, 0.986, 0.987, 0.988, 0.989, 0.990),
        "qwen_rewrite": (0.700, 0.750, 0.800, 0.850, 0.854, 0.860, 0.900, 0.950),
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
        "# 小语种泛化冗余率与阈值选择报告",
        "",
        f"- 数据目录：`{report['data_dir']}`",
        f"- 阈值选择规则：按方法分别扫描阈值；每个国家组和每个单独文件冗余率均需低于 `{report['target_max_redundancy_rate']:.0%}`。",
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


def char_ngram_jaccard(left: str, right: str, *, n: int) -> float:
    left_grams = char_ngrams(left, n)
    right_grams = char_ngrams(right, n)
    if not left_grams and not right_grams:
        return 1.0
    intersection = len(left_grams & right_grams)
    union = len(left_grams | right_grams)
    return intersection / union if union else 0.0


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
