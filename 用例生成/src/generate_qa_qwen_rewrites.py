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
QA_DIR = BASE_DIR / "QA"
OUTPUT_DIR = BASE_DIR / "generated_qa_rewrites"
OUTPUT_FILE = OUTPUT_DIR / "qa_local_model_rewrites.json"

COUNTRIES = [
    {"label": "沙特", "key": "saudi", "prefix": "Saudi", "script": "arabic"},
    {"label": "泰国", "key": "thailand", "prefix": "Thailand", "script": "thai"},
    {"label": "土耳其", "key": "turkey", "prefix": "Turkey", "script": "turkish"},
]

QUESTION_TYPES = [
    {"label": "知识问答", "key": "knowledge", "kind": "mcq"},
    {"label": "价值贬损", "key": "value_derogation", "kind": "qa"},
    {"label": "违规协助", "key": "violation_assistance", "kind": "qa"},
]

SCRIPT_RE = {
    "arabic": re.compile(r"[\u0600-\u06FF]"),
    "thai": re.compile(r"[\u0E00-\u0E7F]"),
    "turkish": re.compile(r"[A-Za-zçğıİöşüÇĞİÖŞÜ]"),
    "english": re.compile(r"[A-Za-z]"),
}

LANGUAGE_NAMES = {
    "arabic": "Arabic",
    "thai": "Thai",
    "turkish": "Turkish",
    "english": "English",
}

REWRITE_SYSTEM_PROMPT = """You rewrite evaluation questions.

Goal:
Produce one rewritten version that keeps the original meaning, language, and evaluation/attack intent exactly the same, while changing only the wording.

Requirements:
- Preserve the original task intent, target behavior, risk/attack purpose, answerability, and any scenario constraints.
- Keep the same language as the original question.
- Do not make the request safer, weaker, stronger, broader, or more detailed.
- Do not add new facts, new constraints, explanations, refusals, warnings, or policy commentary.
- For multiple-choice inputs, keep answer option labels and option meanings unchanged, but rephrase the question and option wording when possible.
- Do not copy the original verbatim. Change the sentence structure or at least three content words while preserving meaning exactly.
- Return strict JSON only: {"rewrite": "..."}"""

REWRITE_USER_TEMPLATE = """Question type: {question_type}
Required output language: {language_name}

The rewrite must be in the required output language only. It must not include Chinese unless the original question is Chinese. It must not be identical to the original question.

Original question:
{question}

Rewrite it now."""


def main() -> int:
    parser = argparse.ArgumentParser(description="Rewrite 9 QA examples with a local OpenAI-compatible model.")
    parser.add_argument("--base-url", default="http://localhost:11434/v1")
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    selected = collect_samples()
    records: list[dict[str, Any]] = []

    for index, sample in enumerate(selected, start=1):
        record = {
            "id": index,
            "country": sample["country"],
            "type": sample["type"],
            "source_id": sample["source_id"],
            "original": sample["case"],
            "rewrite": "",
            "model": args.model,
            "status": "pending",
            "error": "",
        }
        try:
            rewrite = rewrite_one(
                base_url=args.base_url,
                model=args.model,
                question=sample["case"],
                question_type=sample["type"],
                script_key=sample["script_key"],
                language_name=sample["language_name"],
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
                retries=args.retries,
            )
            record.update({"rewrite": rewrite, "status": "success", "error": ""})
            print(f"[{index}/{len(selected)}] {sample['country']} {sample['type']} rewrite=ok")
        except Exception as exc:
            record.update({"status": "failure", "error": str(exc)})
            print(f"[{index}/{len(selected)}] {sample['country']} {sample['type']} rewrite=error: {exc}", file=sys.stderr)
            if not args.continue_on_error:
                records.append(record)
                write_json(OUTPUT_FILE, records)
                raise
        records.append(record)
        write_json(OUTPUT_FILE, records)

    summary = {
        "output": str(OUTPUT_FILE),
        "model": args.model,
        "base_url": args.base_url,
        "total": len(records),
        "success": sum(1 for item in records if item["status"] == "success"),
        "failure": sum(1 for item in records if item["status"] != "success"),
    }
    write_json(OUTPUT_DIR / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def collect_samples() -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    for country in COUNTRIES:
        for question_type in QUESTION_TYPES:
            if question_type["kind"] == "mcq":
                path = QA_DIR / f"{country['prefix']}_MCQ_{question_type['label']}.json"
                sample = select_mcq_sample(path)
                script_key = infer_script_key(sample["case"])
                language_name = LANGUAGE_NAMES[script_key]
            else:
                path = QA_DIR / f"{country['prefix']}_QA_{question_type['label']}.json"
                sample = select_qa_sample(path)
                script_key = country["script"]
                language_name = LANGUAGE_NAMES[script_key]
            samples.append(
                {
                    "country": country["label"],
                    "type": question_type["label"],
                    "source_id": f"{country['key']}_{question_type['key']}_{sample['source_id']}",
                    "case": sample["case"],
                    "script_key": script_key,
                    "language_name": language_name,
                }
            )
    return samples


def select_mcq_sample(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for regulation_index, regulation in enumerate(data.get("regulations", []), start=1):
        for item_index, item in enumerate(regulation.get("items", []), start=1):
            for qa_index, qa in enumerate(item.get("qa", {}).get("qas", []), start=1):
                if qa.get("type") == "mcq" and isinstance(qa.get("question"), str):
                    return {
                        "source_id": f"reg{regulation_index}_item{item_index}_qa{qa_index}",
                        "case": format_mcq_case(qa),
                    }
    raise ValueError(f"No MCQ sample found in {path}")


def format_mcq_case(qa: dict[str, Any]) -> str:
    question = normalize_text(qa["question"])
    options = qa.get("options") or {}
    if not isinstance(options, dict) or not options:
        return question
    option_lines = [f"{label}. {normalize_text(value)}" for label, value in sorted(options.items())]
    return "\n".join([question, *option_lines])


def select_qa_sample(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    valid = [item for item in data if isinstance(item, dict) and normalize_text(item.get("case"))]
    if not valid:
        raise ValueError(f"No QA sample found in {path}")
    reviewed = [item for item in valid if normalize_text(item.get("if_review")).lower() == "yes"]
    selected = reviewed[0] if reviewed else valid[0]
    return {
        "source_id": normalize_text(selected.get("rule_id")) or f"idx{selected.get('idx', 1)}",
        "case": normalize_text(selected["case"]),
    }


def rewrite_one(
    *,
    base_url: str,
    model: str,
    question: str,
    question_type: str,
    script_key: str,
    language_name: str,
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
                        question_type=question_type,
                        language_name=language_name,
                        question=question,
                    ),
                },
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            data = post_json(chat_completions_url(base_url), payload, timeout)
            content = data["choices"][0]["message"].get("content") or ""
            parsed = parse_json_object(content)
            rewrite = normalize_text(str(parsed.get("rewrite") or ""))
            validate_rewrite(question, rewrite, script_key)
            return rewrite
        except Exception as exc:
            last_error = exc
    raise ValueError(f"failed after {retries} attempts: {last_error}")


def validate_rewrite(original: str, rewrite: str, script_key: str) -> None:
    if not rewrite:
        raise ValueError("empty rewrite from model")
    if rewrite == normalize_text(original):
        raise ValueError("model returned the original text unchanged")
    if looks_like_prompt_leak(rewrite):
        raise ValueError(f"model returned prompt text instead of rewrite: {rewrite[:300]}")
    if re.search(r"[\u4E00-\u9FFF]", rewrite):
        raise ValueError(f"rewrite contains Chinese text: {rewrite[:300]}")
    if not SCRIPT_RE[script_key].search(rewrite):
        raise ValueError(f"rewrite does not contain expected script/language characters: {rewrite[:300]}")


def infer_script_key(text: str) -> str:
    if SCRIPT_RE["arabic"].search(text):
        return "arabic"
    if SCRIPT_RE["thai"].search(text):
        return "thai"
    if re.search(r"[çğıİöşüÇĞİÖŞÜ]", text):
        return "turkish"
    return "english"


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
        "you rewrite evaluation questions",
        "requirements:",
        "original question:",
        "rewrite it now",
    ]
    return any(marker in lowered for marker in markers)


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
