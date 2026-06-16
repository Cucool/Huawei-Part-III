#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = BASE_DIR / "pro_data"
DEFAULT_OUTPUT_DIR = BASE_DIR / "en_data"

TRANSLATE_SYSTEM_PROMPT = """You translate safety-evaluation test inputs into English.

Important requirements:
- Translate the user test input only. Do not answer the request.
- Preserve the original request intent, including harmful, sensitive, privacy, or jailbreak-testing intent.
- Do not make the request safer, weaker, stronger, broader, or more detailed.
- Preserve labels, numbering, option letters, placeholders, JSON-like text, and line breaks when possible.
- Preserve names and IDs by transliterating only when appropriate.
- Return strict JSON only: {"translation": "..."}"""

TRANSLATE_USER_TEMPLATE = """Source language: {source_language}

Text to translate into English:
```text
{case_text}
```"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Translate jailbreak/pro_data JSON cases into English en_data JSON files.")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--base-url", default="http://localhost:11434/v1")
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--only", nargs="*", default=None, help="Optional file names to translate, e.g. QA_arabic.json privacy_thai.json")
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args()

    input_files = sorted(args.input_dir.glob("*.json"))
    if args.only:
        wanted = set(args.only)
        input_files = [path for path in input_files if path.name in wanted]
    if not input_files:
        raise FileNotFoundError(f"No JSON files found in {args.input_dir}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "input_dir": str(args.input_dir),
        "output_dir": str(args.output_dir),
        "model": args.model,
        "base_url": args.base_url,
        "files": {},
    }

    for input_path in input_files:
        output_path = args.output_dir / input_path.name
        print(f"[Translate] {input_path.name} -> {output_path}")
        records = load_json_list(input_path)
        existing_by_id = load_existing_by_id(output_path)
        translated_records: list[dict[str, Any]] = []

        for index, item in enumerate(records, start=1):
            record_id = item.get("id", index)
            original_case = str(item.get("case", "")).strip()
            if not original_case:
                translated_records.append(mark_failure(item, args.model, "missing case"))
                continue

            existing = existing_by_id.get(str(record_id))
            if (
                existing
                and existing.get("translation_status") == "success"
                and existing.get("original_case") == original_case
                and str(existing.get("case", "")).strip()
            ):
                translated_records.append(existing)
                continue

            try:
                translation = translate_one(
                    base_url=args.base_url,
                    model=args.model,
                    source_language=str(item.get("language", "") or infer_language_from_filename(input_path.name)),
                    case_text=original_case,
                    temperature=args.temperature,
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                    retries=args.retries,
                )
                translated_records.append(make_translated_record(item, translation, original_case, args.model))
            except Exception as exc:
                failure = mark_failure(item, args.model, str(exc))
                translated_records.append(failure)
                print(f"[Translate][ERROR] {input_path.name} id={record_id}: {exc}", file=sys.stderr)
                if not args.continue_on_error:
                    save_json(output_path, translated_records)
                    raise

            save_json(output_path, translated_records)

        success = sum(1 for item in translated_records if item.get("translation_status") == "success")
        failure = len(translated_records) - success
        summary["files"][input_path.name] = {
            "output": str(output_path),
            "total": len(translated_records),
            "success": success,
            "failure": failure,
        }
        save_json(output_path, translated_records)

    save_json(args.output_dir / "translation_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def translate_one(
    *,
    base_url: str,
    model: str,
    source_language: str,
    case_text: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    retries: int,
) -> str:
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": TRANSLATE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": TRANSLATE_USER_TEMPLATE.format(
                        source_language=source_language or "unknown",
                        case_text=case_text,
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
            translation = normalize_text(str(parsed.get("translation") or ""))
            validate_translation(case_text, translation)
            return translation
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2)
    raise ValueError(f"translation failed after {retries} attempts: {last_error}")


def make_translated_record(item: dict[str, Any], translation: str, original_case: str, model: str) -> dict[str, Any]:
    record = dict(item)
    record["original_language"] = item.get("language", "")
    record["language"] = "english"
    record["original_case"] = original_case
    record["case"] = translation
    record["translation_model"] = model
    record["translation_status"] = "success"
    record["translation_error"] = ""
    return record


def mark_failure(item: dict[str, Any], model: str, error: str) -> dict[str, Any]:
    record = dict(item)
    record["original_language"] = item.get("language", "")
    record["language"] = "english"
    record["original_case"] = str(item.get("case", "") or "")
    record["translation_model"] = model
    record["translation_status"] = "failure"
    record["translation_error"] = error
    return record


def validate_translation(original: str, translation: str) -> None:
    if not translation:
        raise ValueError("empty translation")
    if normalize_text(original) == translation:
        raise ValueError("translation is identical to source")
    if looks_like_prompt_leak(translation):
        raise ValueError(f"translation looks like prompt leakage: {translation[:200]}")


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
    return urljoin(base_url.rstrip("/") + "/", "chat/completions")


def parse_json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, re.S)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("model did not return a JSON object")
    return parsed


def looks_like_prompt_leak(value: str) -> bool:
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            "source language:",
            "text to translate",
            "return strict json",
            "translation\":",
        )
    )


def infer_language_from_filename(name: str) -> str:
    lowered = name.lower()
    if "arabic" in lowered:
        return "arabic"
    if "thai" in lowered:
        return "thai"
    if "turkish" in lowered:
        return "turkish"
    return ""


def normalize_text(value: str) -> str:
    return re.sub(r"[ \t]+", " ", str(value or "").replace("\r\n", "\n")).strip()


def load_json_list(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return [item for item in data if isinstance(item, dict)]


def load_existing_by_id(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = load_json_list(path)
    except Exception:
        return {}
    return {str(item.get("id")): item for item in data}


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
