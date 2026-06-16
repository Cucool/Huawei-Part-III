#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "generated_low_resource_cases"

LANGUAGES = {
    "thai": {
        "template_file": BASE_DIR / "th.txt",
        "category_label": "privacy",
        "script_re": re.compile(r"[\u0E00-\u0E7F]"),
    },
    "turkish": {
        "template_file": BASE_DIR / "tu.txt",
        "category_label": "privacy",
        "script_re": re.compile(r"[A-Za-zçğıİöşüÇĞİÖŞÜ]"),
    },
    "arabic": {
        "template_file": BASE_DIR / "ar.txt",
        "category_label": "privacy",
        "script_re": re.compile(r"[\u0600-\u06FF]"),
    },
}

REWRITE_SYSTEM_PROMPT = """You are rewriting a single user question.

Goal:
Rewrite the user's task into one final transformed question that preserves the original intent and semantic meaning while applying the specified paraphrase types in a controlled way.

Paraphrase taxonomy:
- Lexical Substitution: Replace words or short phrases with semantically equivalent alternatives when natural.
- Syntactic Reordering: Adjust word order or sentence structure while keeping the meaning unchanged.
- Morphological Variation: Change grammatical form or expression style when possible without altering intent.
- Referential Reformulation: Rephrase named entities, titles, or referring expressions in a comparable and natural way when possible.
- Light Addition/Deletion: Add or remove minor non-essential surface details only if the core request remains exactly the same.

Requirements:
- Preserve the user's original task and request intent exactly.
- Keep the transformed question semantically equivalent to the original.
- Apply the selected paraphrase types naturally and fluently.
- Do not introduce new facts, constraints, or requests.
- Keep the output in the same language as the user task.
- Return strict JSON only: {"rewrite": "..."}"""

REWRITE_USER_TEMPLATE = """User task:
{question}

Category: {category_label}"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Rewrite privacy templates with local Qwen via OpenAI-compatible API.")
    parser.add_argument("--base-url", default="http://localhost:11434/v1")
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--language", choices=["all", *LANGUAGES], default="all")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--only-failures", action="store_true", help="Only retry failed records in existing output files.")
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    selected = LANGUAGES if args.language == "all" else {args.language: LANGUAGES[args.language]}
    summary: dict[str, Any] = {}

    for language, spec in selected.items():
        templates = load_templates(spec["template_file"])
        output_path = OUTPUT_DIR / f"{language}_privacy_qwen_rewrite.json"
        if args.only_failures and output_path.exists():
            records = json.loads(output_path.read_text(encoding="utf-8"))
            targets = [item for item in records if item.get("status") != "success"]
        else:
            records = [
                {
                    "language": language,
                    "group": "privacy_template",
                    "method": "qwen_rewrite",
                    "model": args.model,
                    "template_index": index,
                    "category_label": spec["category_label"],
                    "original": template,
                    "rewrite": "",
                    "status": "pending",
                    "error": "",
                }
                for index, template in enumerate(templates, start=1)
            ]
            targets = records

        if not targets:
            print(f"[{language}] no failed records to retry")

        for item in targets:
            index = int(item["template_index"])
            template = str(item["original"])
            try:
                rewrite = rewrite_one(
                    base_url=args.base_url,
                    model=args.model,
                    question=template,
                    category_label=spec["category_label"],
                    script_re=spec["script_re"],
                    temperature=args.temperature,
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                    retries=args.retries,
                )
                item.update({
                    "model": args.model,
                    "rewrite": rewrite,
                    "status": "success",
                    "error": "",
                })
                print(f"[{language} {index}/{len(templates)}] rewrite=ok")
            except Exception as exc:
                item.update({
                    "model": args.model,
                    "rewrite": "",
                    "status": "failure",
                    "error": str(exc),
                })
                print(f"[{language} {index}/{len(templates)}] rewrite=error: {exc}", file=sys.stderr)
                if not args.continue_on_error:
                    write_json(output_path, records)
                    raise

        write_json(output_path, records)
        summary[language] = {
            "output_file": str(output_path),
            "model": args.model,
            "base_url": args.base_url,
            "total": len(records),
            "success": sum(1 for item in records if item["status"] == "success"),
            "failure": sum(1 for item in records if item["status"] != "success"),
        }

    summary_path = OUTPUT_DIR / "privacy_qwen_rewrite_summary.json"
    write_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def load_templates(path: Path) -> list[str]:
    templates = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(templates) < 10:
        raise ValueError(f"{path} must contain at least 10 templates")
    return templates[:10]


def rewrite_one(
    *,
    base_url: str,
    model: str,
    question: str,
    category_label: str,
    script_re: re.Pattern[str],
    temperature: float,
    max_tokens: int,
    timeout: float,
    retries: int,
) -> str:
    question_variants = [protect_placeholders(question), question]
    last_error: Exception | None = None
    for prompt_question in dict.fromkeys(question_variants):
        for _ in range(max(1, retries)):
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                    {"role": "user", "content": REWRITE_USER_TEMPLATE.format(question=prompt_question, category_label=category_label)},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            try:
                data = post_json(chat_completions_url(base_url), payload, timeout)
                content = data["choices"][0]["message"].get("content") or ""
                parsed = parse_json_object(content)
                rewrite = restore_placeholders(str(parsed.get("rewrite") or "").strip())
                validate_rewrite(question, rewrite, script_re)
                return rewrite
            except Exception as exc:
                last_error = exc
    raise ValueError(f"failed after {retries} attempts per prompt variant: {last_error}")


PLACEHOLDER_PROTECTION = {
    "【sb】": "__PERSON_SLOT__",
    "【sth】": "__ATTRIBUTE_SLOT__",
}


def protect_placeholders(text: str) -> str:
    for original, protected in PLACEHOLDER_PROTECTION.items():
        text = text.replace(original, protected)
    return text


def restore_placeholders(text: str) -> str:
    for original, protected in PLACEHOLDER_PROTECTION.items():
        text = text.replace(protected, original)
        text = text.replace(protected.lower(), original)
    person_variants = [
        r"__\s*PERSON\s*_\s*SLOT\s*__",
        r"__\s*PERSON\s*_\s*NAME\s*__",
        r"__\s*PERSON\s*__",
    ]
    attribute_variants = [
        r"__\s*ATTRIBUTE\s*_\s*SLOT\s*__",
        r"__\s*ATTRIBUTE\s*_\s*NAME\s*__",
        r"__\s*ATTRIBUTE\s*__",
    ]
    for pattern in person_variants:
        text = re.sub(pattern, "【sb】", text, flags=re.IGNORECASE)
    for pattern in attribute_variants:
        text = re.sub(pattern, "【sth】", text, flags=re.IGNORECASE)
    return text


def validate_rewrite(original: str, rewrite: str, script_re: re.Pattern[str]) -> None:
    if not rewrite:
        raise ValueError("empty rewrite from model")
    if looks_like_prompt_leak(rewrite):
        raise ValueError(f"model returned prompt text instead of rewrite: {rewrite[:300]}")
    original_placeholders = sorted(re.findall(r"【[^】]+】", original))
    rewrite_placeholders = sorted(re.findall(r"【[^】]+】", rewrite))
    if original_placeholders != rewrite_placeholders:
        raise ValueError(f"placeholder mismatch: expected {original_placeholders}, got {rewrite_placeholders}; rewrite={rewrite[:300]}")
    if not script_re.search(rewrite):
        raise ValueError(f"rewrite does not contain target-language script: {rewrite[:300]}")
    if re.search(r"[\u4E00-\u9FFF]", rewrite):
        raise ValueError(f"rewrite contains Chinese text: {rewrite[:300]}")


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
    data, _ = json.JSONDecoder().raw_decode(cleaned)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")
    return data


def looks_like_prompt_leak(text: str) -> bool:
    lowered = text.lower()
    markers = [
        "you are rewriting",
        "paraphrase taxonomy",
        "requirements:",
        "user task:",
        "category:",
    ]
    return any(marker in lowered for marker in markers)


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
