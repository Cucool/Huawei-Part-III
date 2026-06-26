from __future__ import annotations

import argparse
import csv
import json
import math
import re
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

PREFIX = r"(?:\[To review\]\s*|【需审核】\s*)?"
RULE_RE = re.compile(rf"^{PREFIX}(?:Rule|规则)\s*(\d+)\s*[:：]")
RULE_APP_RE = re.compile(r"^(?:Rule\s*\d+\s+application examples|规则\s*\d+\s*的应用示例)\s*[:：]?", re.I)
ORIGINAL_RE = re.compile(r"^(?:Original sentence|原句子)\s*(\d+)\s*[:：]")
SEPARATOR_RE = re.compile(r"^=+$")
TRAILING_METADATA_RE = re.compile(r"^(?:Expert Reviewer's Signature|Date)\s*[:：]", re.I)
WORD_TOKEN_RE = re.compile(r"(?:【[^】]+】|`[^`]+`|\w+)(?:['’_-]\w+)*|[^\w\s]", re.UNICODE)
REVIEWER_NOTE_PAREN_RE = re.compile(
    r"\s*[\(（]\s*(?:(?:note|备注|注)\s*[:：;；]|same\s+situation\b|same\s+problem\b)[^()（）]*[\)）]",
    re.I,
)
REVIEWER_NOTE_LINE_RE = re.compile(r"^\s*(?:note|备注|注)\s*[:：;；].*$", re.I)

TARGET_RE_BY_KIND = {
    "dialect": re.compile(rf"^{PREFIX}(?:Partially dialectized sentence|部分方言化的句子)\s*(\d+)\s*[:：]"),
    "rewrite": re.compile(rf"^{PREFIX}(?:Rewritten sentence|改写后的句子)\s*(\d+)\s*[:：]"),
    "translation": re.compile(rf"^{PREFIX}(?:Translated sentence|翻译后的句子)\s*(\d+)\s*[:：]"),
}


@dataclass
class ReviewTarget:
    key: str
    kind: str
    rule_index: int | None
    item_index: int
    original_sentence: str
    review_text: str


def infer_kind(filename: str) -> str:
    if "部分方言化" in filename or "Dialectization" in filename:
        return "dialect"
    if "同义改写" in filename or "Synonym" in filename:
        return "rewrite"
    if "句子翻译" in filename or "Translation" in filename:
        return "translation"
    raise ValueError(f"无法识别 docx 类型: {filename}")


def normalize_for_compare(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_for_char_count(text: str) -> str:
    return re.sub(r"\s+", "", normalize_for_compare(text))


def char_len(text: str) -> int:
    return len(normalize_for_char_count(text))


def tokenize_for_word_metrics(text: str) -> list[str]:
    return WORD_TOKEN_RE.findall(normalize_for_compare(text))


def levenshtein_distance(a: str, b: str) -> int:
    a = normalize_for_char_count(a)
    b = normalize_for_char_count(b)

    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (ca != cb),
                )
            )
        previous = current

    return previous[-1]


def token_levenshtein_distance(source_tokens: list[str], feedback_tokens: list[str]) -> int:
    if source_tokens == feedback_tokens:
        return 0
    if not source_tokens:
        return len(feedback_tokens)
    if not feedback_tokens:
        return len(source_tokens)

    previous = list(range(len(feedback_tokens) + 1))
    for i, source_token in enumerate(source_tokens, start=1):
        current = [i]
        for j, feedback_token in enumerate(feedback_tokens, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (source_token != feedback_token),
                )
            )
        previous = current

    return previous[-1]


def token_bag_content_ops(source_tokens: list[str], feedback_tokens: list[str]) -> int:
    source_counter = Counter(source_tokens)
    feedback_counter = Counter(feedback_tokens)
    tokens = set(source_counter) | set(feedback_counter)

    deletions = sum(max(source_counter[token] - feedback_counter[token], 0) for token in tokens)
    insertions = sum(max(feedback_counter[token] - source_counter[token], 0) for token in tokens)
    return max(deletions, insertions)


def ter_shift_modified_tokens(source_text: str, feedback_text: str, shift_penalty: float) -> int:
    source_tokens = tokenize_for_word_metrics(source_text)
    feedback_tokens = tokenize_for_word_metrics(feedback_text)
    edit_distance = token_levenshtein_distance(source_tokens, feedback_tokens)
    if edit_distance == 0:
        return 0

    content_ops = min(edit_distance, token_bag_content_ops(source_tokens, feedback_tokens))
    reorder_only_ops = max(0, edit_distance - content_ops)
    adjusted = content_ops + reorder_only_ops * shift_penalty
    return max(1, math.ceil(adjusted))


def tokenize_for_reorder_discount(text: str) -> list[str]:
    text = normalize_for_compare(text)
    tokens: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            tokens.append("".join(current))
            current.clear()

    for ch in text:
        if ch.isspace():
            flush()
        elif ch.isalnum() or ch in {"_", "'", "’", "-"}:
            current.append(ch)
        else:
            flush()
            tokens.append(ch)

    flush()
    return tokens


def token_bag_distance(a: str, b: str) -> int:
    a_counter = Counter(tokenize_for_reorder_discount(a))
    b_counter = Counter(tokenize_for_reorder_discount(b))

    tokens = set(a_counter) | set(b_counter)
    return sum(abs(a_counter[token] - b_counter[token]) * len(token) for token in tokens)


def adjusted_modified_chars(
    source_text: str,
    feedback_text: str,
    *,
    doc_kind: str,
    item_index: int,
    translation_reorder_start: int,
    reorder_penalty: float,
) -> int:
    char_distance = levenshtein_distance(source_text, feedback_text)
    if char_distance == 0:
        return 0

    should_discount_reorder = doc_kind == "translation" and item_index >= translation_reorder_start
    if not should_discount_reorder:
        return char_distance

    bag_distance = token_bag_distance(source_text, feedback_text)
    content_change = min(char_distance, bag_distance)
    reorder_only_change = max(0, char_distance - content_change)
    adjusted = content_change + reorder_only_change * reorder_penalty
    return max(1, math.ceil(adjusted))


def char_ngrams(text: str, n: int) -> Counter[str]:
    text = normalize_for_compare(text)
    if not text:
        return Counter()
    if len(text) <= n:
        return Counter([text])
    return Counter(text[index : index + n] for index in range(len(text) - n + 1))


def ngram_modified_chars(source_text: str, feedback_text: str, n: int) -> int:
    source_grams = char_ngrams(source_text, n)
    feedback_grams = char_ngrams(feedback_text, n)
    if not source_grams and not feedback_grams:
        return 0
    if source_grams == feedback_grams:
        return 0

    overlap = sum((source_grams & feedback_grams).values())
    denominator = max(sum(source_grams.values()), sum(feedback_grams.values()))
    if denominator == 0:
        return max(char_len(source_text), char_len(feedback_text))

    diff_rate = 1 - overlap / denominator
    return math.ceil(char_len(source_text) * diff_rate)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def w_bool_is_enabled(value: str | None) -> bool:
    return value is None or value.lower() not in {"0", "false", "off", "none"}


def run_has_strikethrough(run: ET.Element) -> bool:
    for run_properties in run.findall(f"{{{W_NS}}}rPr"):
        for prop in run_properties:
            if local_name(prop.tag) in {"strike", "dstrike"}:
                return w_bool_is_enabled(prop.attrib.get(f"{{{W_NS}}}val"))
    return False


def visible_text_after_accepting_revisions(elem: ET.Element, in_deleted: bool = False) -> str:
    name = local_name(elem.tag)
    if name in {"del", "delText", "moveFrom"}:
        in_deleted = True
    elif name == "r" and run_has_strikethrough(elem):
        in_deleted = True

    parts: list[str] = []

    if not in_deleted:
        if name == "t":
            parts.append(elem.text or "")
        elif name == "tab":
            parts.append("\t")
        elif name in {"br", "cr"}:
            parts.append("\n")

    for child in elem:
        parts.append(visible_text_after_accepting_revisions(child, in_deleted))

    return "".join(parts)


def docx_paragraphs_after_accepting_revisions(docx_path: Path) -> list[str]:
    with zipfile.ZipFile(docx_path) as zf:
        xml_bytes = zf.read("word/document.xml")

    root = ET.fromstring(xml_bytes)
    paragraphs: list[str] = []

    for paragraph in root.iter(f"{{{W_NS}}}p"):
        text = visible_text_after_accepting_revisions(paragraph)
        text = text.replace("\u00a0", " ").strip()
        if text:
            paragraphs.append(text)

    return paragraphs


def strip_reviewer_notes(text: str) -> str:
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        if REVIEWER_NOTE_LINE_RE.match(line):
            continue
        line = REVIEWER_NOTE_PAREN_RE.sub("", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def txt_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def target_re(kind: str) -> re.Pattern[str]:
    return TARGET_RE_BY_KIND[kind]


def is_any_marker(line: str, kind: str) -> bool:
    return (
        bool(RULE_RE.match(line))
        or bool(RULE_APP_RE.match(line))
        or bool(ORIGINAL_RE.match(line))
        or bool(target_re(kind).match(line))
        or bool(SEPARATOR_RE.match(line))
        or bool(TRAILING_METADATA_RE.match(line))
    )


def clean_block(lines: list[str]) -> str:
    return "\n".join(line.strip() for line in lines if line.strip()).strip()


def extract_targets(lines: list[str], kind: str, include_rules: bool) -> list[ReviewTarget]:
    targets: list[ReviewTarget] = []
    i = 0
    current_rule: int | None = None

    while i < len(lines):
        line = lines[i]

        if kind == "dialect":
            rule_match = RULE_RE.match(line)
            if rule_match:
                current_rule = int(rule_match.group(1))
                i += 1

                rule_lines: list[str] = []
                while i < len(lines):
                    if RULE_APP_RE.match(lines[i]) or ORIGINAL_RE.match(lines[i]) or RULE_RE.match(lines[i]):
                        break
                    if not SEPARATOR_RE.match(lines[i]):
                        rule_lines.append(lines[i])
                    i += 1

                rule_text = clean_block(rule_lines)
                if include_rules and rule_text:
                    targets.append(
                        ReviewTarget(
                            key=f"rule:{current_rule}",
                            kind="rule",
                            rule_index=current_rule,
                            item_index=current_rule,
                            original_sentence="",
                            review_text=rule_text,
                        )
                    )
                continue

        original_match = ORIGINAL_RE.match(line)
        if not original_match:
            i += 1
            continue

        original_index = int(original_match.group(1))
        i += 1

        original_lines: list[str] = []
        while i < len(lines) and not target_re(kind).match(lines[i]):
            if RULE_RE.match(lines[i]) or ORIGINAL_RE.match(lines[i]) or SEPARATOR_RE.match(lines[i]):
                break
            original_lines.append(lines[i])
            i += 1

        original_sentence = clean_block(original_lines)
        if i >= len(lines):
            break

        review_match = target_re(kind).match(lines[i])
        if not review_match:
            continue

        review_index = int(review_match.group(1))
        i += 1

        review_lines: list[str] = []
        while i < len(lines) and not is_any_marker(lines[i], kind):
            review_lines.append(lines[i])
            i += 1

        review_text = clean_block(review_lines)
        if kind == "dialect":
            if current_rule is None:
                raise ValueError(f"方言化示例缺少当前规则: original sentence {original_index}")
            key = f"sentence:rule:{current_rule}:example:{review_index}"
            rule_index = current_rule
        else:
            key = f"sentence:{review_index}"
            rule_index = None

        targets.append(
            ReviewTarget(
                key=key,
                kind="sentence",
                rule_index=rule_index,
                item_index=review_index,
                original_sentence=original_sentence,
                review_text=review_text,
            )
        )

    return targets


def empty_bucket() -> dict[str, int]:
    return {
        "total": 0,
        "changed": 0,
        "source_chars": 0,
        "source_tokens": 0,
        "simple_modified_chars": 0,
        "word_modified_tokens": 0,
        "ter_shift_modified_tokens": 0,
        "translation_adjusted_modified_chars": 0,
        "translation_ngram_modified_chars": 0,
    }


def compare_docx_with_source(
    docx_path: Path,
    source_txt_path: Path,
    *,
    translation_reorder_start: int,
    reorder_penalty: float,
    ngram_n: int,
) -> dict:
    kind = infer_kind(docx_path.name)

    source_targets = extract_targets(txt_lines(source_txt_path), kind, include_rules=False)
    feedback_targets = extract_targets(
        docx_paragraphs_after_accepting_revisions(docx_path),
        kind,
        include_rules=False,
    )
    feedback_by_key = {item.key: item for item in feedback_targets}

    bucket = empty_bucket()
    missing: list[str] = []
    details: list[dict[str, object]] = []

    for source in source_targets:
        feedback = feedback_by_key.get(source.key)
        source_chars = char_len(source.review_text)
        source_tokens = tokenize_for_word_metrics(source.review_text)
        source_token_count = len(source_tokens)

        if feedback is None:
            missing.append(source.key)
            feedback_text = ""
            raw_feedback_text = ""
            simple_chars = 0
            word_tokens = 0
            ter_tokens = 0
            adjusted_chars = None
            ngram_chars = None
            original_matches = None
        else:
            raw_feedback_text = feedback.review_text
            feedback_text = strip_reviewer_notes(raw_feedback_text)
            simple_chars = levenshtein_distance(source.review_text, feedback_text)
            feedback_tokens = tokenize_for_word_metrics(feedback_text)
            word_tokens = token_levenshtein_distance(source_tokens, feedback_tokens)
            ter_tokens = ter_shift_modified_tokens(
                source.review_text,
                feedback_text,
                reorder_penalty,
            )
            if kind == "translation":
                adjusted_chars = adjusted_modified_chars(
                    source.review_text,
                    feedback_text,
                    doc_kind=kind,
                    item_index=source.item_index,
                    translation_reorder_start=translation_reorder_start,
                    reorder_penalty=reorder_penalty,
                )
                ngram_chars = ngram_modified_chars(source.review_text, feedback_text, ngram_n)
            else:
                adjusted_chars = None
                ngram_chars = None
            original_matches = normalize_for_compare(source.original_sentence) == normalize_for_compare(
                feedback.original_sentence
            )

        changed = word_tokens > 0
        bucket["total"] += 1
        bucket["changed"] += int(changed)
        bucket["source_chars"] += source_chars
        bucket["source_tokens"] += source_token_count
        bucket["simple_modified_chars"] += simple_chars
        bucket["word_modified_tokens"] += word_tokens
        bucket["ter_shift_modified_tokens"] += ter_tokens
        if adjusted_chars is not None:
            bucket["translation_adjusted_modified_chars"] += adjusted_chars
        if ngram_chars is not None:
            bucket["translation_ngram_modified_chars"] += ngram_chars

        details.append(
            {
                "docx": docx_path.name,
                "key": source.key,
                "kind": source.kind,
                "rule_index": source.rule_index,
                "item_index": source.item_index,
                "changed": changed,
                "source_chars": source_chars,
                "simple_modified_chars": simple_chars,
                "simple_char_modification_rate": simple_chars / source_chars if source_chars else 0,
                "source_tokens": source_token_count,
                "word_modified_tokens": word_tokens,
                "word_modification_rate": word_tokens / source_token_count if source_token_count else 0,
                "ter_shift_modified_tokens": ter_tokens,
                "ter_shift_modification_rate": ter_tokens / source_token_count if source_token_count else 0,
                "translation_adjusted_modified_chars": adjusted_chars,
                "translation_adjusted_char_modification_rate": (
                    adjusted_chars / source_chars if adjusted_chars is not None and source_chars else None
                ),
                "ngram_n": ngram_n,
                "translation_ngram_modified_chars": ngram_chars,
                "translation_ngram_char_modification_rate": (
                    ngram_chars / source_chars if ngram_chars is not None and source_chars else None
                ),
                "original_matches": original_matches,
                "original_sentence": source.original_sentence,
                "source_review_text": source.review_text,
                "raw_feedback_review_text": raw_feedback_text,
                "feedback_review_text": feedback_text,
            }
        )

    result: dict[str, object] = {
        "docx": str(docx_path),
        "source_txt": str(source_txt_path),
        "kind": kind,
        "missing_keys_in_feedback": missing,
        "details": details,
    }
    result["sentence_total"] = bucket["total"]
    result["sentence_changed"] = bucket["changed"]
    result["sentence_modification_rate"] = bucket["changed"] / bucket["total"] if bucket["total"] else 0
    result["sentence_source_chars"] = bucket["source_chars"]
    result["sentence_simple_modified_chars"] = bucket["simple_modified_chars"]
    result["sentence_simple_char_modification_rate"] = (
        bucket["simple_modified_chars"] / bucket["source_chars"] if bucket["source_chars"] else 0
    )
    result["sentence_source_tokens"] = bucket["source_tokens"]
    result["sentence_word_modified_tokens"] = bucket["word_modified_tokens"]
    result["sentence_word_modification_rate"] = (
        bucket["word_modified_tokens"] / bucket["source_tokens"] if bucket["source_tokens"] else 0
    )
    result["sentence_ter_shift_modified_tokens"] = bucket["ter_shift_modified_tokens"]
    result["sentence_ter_shift_modification_rate"] = (
        bucket["ter_shift_modified_tokens"] / bucket["source_tokens"] if bucket["source_tokens"] else 0
    )
    if kind == "translation":
        result["sentence_translation_adjusted_modified_chars"] = bucket["translation_adjusted_modified_chars"]
        result["sentence_translation_adjusted_char_modification_rate"] = (
            bucket["translation_adjusted_modified_chars"] / bucket["source_chars"] if bucket["source_chars"] else 0
        )
        result["sentence_translation_ngram_modified_chars"] = bucket["translation_ngram_modified_chars"]
        result["sentence_translation_ngram_char_modification_rate"] = (
            bucket["translation_ngram_modified_chars"] / bucket["source_chars"] if bucket["source_chars"] else 0
        )
    else:
        result["sentence_translation_adjusted_modified_chars"] = None
        result["sentence_translation_adjusted_char_modification_rate"] = None
        result["sentence_translation_ngram_modified_chars"] = None
        result["sentence_translation_ngram_char_modification_rate"] = None

    return result


def find_source_txt(source_dir: Path, docx_path: Path) -> Path:
    source_txt = source_dir / f"{docx_path.stem}.txt"
    if not source_txt.exists():
        raise FileNotFoundError(f"找不到对应 txt 原稿: {source_txt}")
    return source_txt


def format_optional_value(value: object) -> str:
    return "-" if value is None else str(value)


def format_optional_rate(value: object) -> str:
    return "-" if value is None else f"{float(value):.2%}"


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    parser = argparse.ArgumentParser()
    parser.add_argument("--feedback-dir", type=Path, default=repo_root / "专家反馈文件" / "土耳其")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=repo_root / "专家审核文件生成" / "generated_expert_review_docs" / "第三阶段-专家审核文件" / "土耳其",
    )
    parser.add_argument("--translation-reorder-start", type=int, default=38)
    parser.add_argument("--reorder-penalty", type=float, default=0.15)
    parser.add_argument("--ngram-n", type=int, default=3)
    parser.add_argument("--json-output", type=Path, default=None)
    parser.add_argument("--csv-output", type=Path, default=None)
    args = parser.parse_args()

    results = []
    all_details = []

    for docx_path in sorted(args.feedback_dir.glob("*.docx")):
        if docx_path.name.startswith((".", "~$")):
            continue
        source_txt = find_source_txt(args.source_dir, docx_path)
        result = compare_docx_with_source(
            docx_path,
            source_txt,
            translation_reorder_start=args.translation_reorder_start,
            reorder_penalty=args.reorder_penalty,
            ngram_n=args.ngram_n,
        )
        results.append({key: value for key, value in result.items() if key != "details"})
        all_details.extend(result["details"])

    print(
        "docx\t"
        "sentence_total\tsentence_changed\tsentence_rate\t"
        "source_tokens\tword_edit_tokens\tword_edit_rate\t"
        "ter_shift_tokens\tter_shift_rate"
    )
    for row in results:
        print(
            f"{Path(str(row['docx'])).name}\t"
            f"{row['sentence_total']}\t{row['sentence_changed']}\t"
            f"{row['sentence_modification_rate']:.2%}\t"
            f"{row['sentence_source_tokens']}\t"
            f"{row['sentence_word_modified_tokens']}\t"
            f"{row['sentence_word_modification_rate']:.2%}\t"
            f"{row['sentence_ter_shift_modified_tokens']}\t"
            f"{row['sentence_ter_shift_modification_rate']:.2%}"
        )

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps({"summary": results, "details": all_details}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if args.csv_output:
        args.csv_output.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = list(all_details[0].keys()) if all_details else []
        with args.csv_output.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_details)


if __name__ == "__main__":
    main()
