from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable


JAILBREAK_DIR = Path(__file__).resolve().parent
SAVE_EVERY = 200
CASE_TEXT_KEYS = ("case", "prompt", "Question", "question", "text", "content", "query", "q")
LANGUAGE_ALIASES = {
    "english": "english",
    "en": "english",
    "arabic": "arabic",
    "ar": "arabic",
    "saudi": "arabic",
    "saudi_arabic": "arabic",
    "沙特": "arabic",
    "thai": "thai",
    "th": "thai",
    "thailand": "thai",
    "泰国": "thai",
    "turkish": "turkish",
    "tr": "turkish",
    "turkey": "turkish",
    "土耳其": "turkish",
}
LANGUAGE_NAMES = {
    "english": "English",
    "arabic": "Arabic",
    "thai": "Thai",
    "turkish": "Turkish",
}
METHOD_ALIASES = {
    "JailCon": {"HILL"},
    "RA-DRI": {"PastTenseAttack"},
}


def sanitize_model_name(model_name: str) -> str:
    value = str(model_name or "").strip()
    if not value:
        return "unknown_model"
    return re.sub(r"[\\/:\s]+", "_", value)


def normalize_language(language: Any) -> str:
    value = str(language or "").strip().lower().replace("-", "_").replace(" ", "_")
    return LANGUAGE_ALIASES.get(value, "")


def language_name(language: Any) -> str:
    normalized = normalize_language(language)
    return LANGUAGE_NAMES.get(normalized, "")


def localized_value(mapping: dict[str, str], language: Any, default_key: str = "default") -> str:
    normalized = normalize_language(language)
    return mapping.get(normalized) or mapping[default_key]


def load_json_records(path: str | Path) -> list[dict[str, Any]]:
    input_path = Path(path)
    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        list_values = [value for value in data.values() if isinstance(value, list)]
        data = list_values[0] if list_values else [data]
    if not isinstance(data, list):
        raise ValueError(f"{input_path} 的 JSON 顶层必须是 list 或包含 list 的 dict。")

    records = [item for item in data if isinstance(item, dict)]
    return records


def extract_case_text(item: dict[str, Any]) -> str:
    for key in CASE_TEXT_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(item, ensure_ascii=False)


def infer_category(path: Path, item: dict[str, Any]) -> str:
    existing = item.get("category")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    lowered = str(path).lower()
    if "privacy" in lowered or "隐私" in lowered or item.get("item"):
        return "privacy"
    if "copyright" in lowered or "版权" in lowered:
        return "copyright"
    if "qa" in lowered or item.get("rule_zh"):
        return "QA"
    return "unknown"


def normalize_scheduled_record(
    item: dict[str, Any],
    *,
    input_path: Path,
    fallback_index: int,
) -> dict[str, Any]:
    raw_id = item.get("id", fallback_index)
    try:
        case_id = int(raw_id)
    except (TypeError, ValueError):
        case_id = fallback_index

    raw_attack_id = item.get("attack_id", item.get("method_id"))
    try:
        attack_id = int(raw_attack_id) if raw_attack_id is not None else 0
    except (TypeError, ValueError):
        attack_id = 0

    attack_method = item.get("attack_method") or item.get("method_name") or ""
    if not isinstance(attack_method, str):
        attack_method = str(attack_method)

    return {
        "id": case_id,
        "attack_id": attack_id,
        "attack_method": attack_method.strip(),
        "case": extract_case_text(item),
        "category": infer_category(input_path, item),
        "language": normalize_language(item.get("language", "")),
    }


def load_scheduled_cases(
    input_json: str | Path,
    *,
    expected_attack_method: str | None = None,
) -> list[dict[str, Any]]:
    input_path = Path(input_json)
    records = [
        normalize_scheduled_record(item, input_path=input_path, fallback_index=index)
        for index, item in enumerate(load_json_records(input_path), start=1)
    ]

    if expected_attack_method:
        allowed_methods = {expected_attack_method, *METHOD_ALIASES.get(expected_attack_method, set())}
        mismatched = [
            item
            for item in records
            if item.get("attack_method") and item.get("attack_method") not in allowed_methods
        ]
        if mismatched:
            first = mismatched[0]
            raise ValueError(
                f"{input_path} 包含非 {expected_attack_method} 的样本: "
                f"id={first.get('id')} attack_method={first.get('attack_method')}"
            )

    return records


def output_path_for(
    method_name: str,
    model_name: str,
    input_json: str | Path,
    output_root: str | Path | None = None,
) -> Path:
    root = Path(output_root) if output_root else JAILBREAK_DIR
    return root / f"attack_{method_name}" / sanitize_model_name(model_name) / Path(input_json).name


def load_existing_by_id(output_path: str | Path | None) -> dict[int, dict[str, Any]]:
    if output_path is None:
        return {}
    path = Path(output_path)
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, list):
        return {}

    existing: dict[int, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            case_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        existing[case_id] = item
    return existing


def save_json_atomic(data: Any, output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def save_results_by_id(results_by_id: dict[int, dict[str, Any]], output_path: str | Path) -> None:
    save_json_atomic([results_by_id[i] for i in sorted(results_by_id)], output_path)


def existing_record_matches_source(existing: dict[str, Any], source: dict[str, Any], method_name: str) -> bool:
    if str(existing.get("case", "")).strip() != str(source.get("case", "")).strip():
        return False
    if str(existing.get("category", "")).strip() != str(source.get("category", "")).strip():
        return False
    existing_method = str(existing.get("attack_method", "")).strip()
    if existing_method and existing_method != method_name:
        return False
    return True


def render_messages(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "user"))
        content = str(msg.get("content", ""))
        parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts)


def strip_language_check_exempt_text(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^<>]*>", " ", text)
    text = re.sub(r"\[(system|user|assistant|model)\]", " ", text, flags=re.IGNORECASE)

    code_tokens = (
        "def ",
        "class ",
        "import ",
        "from ",
        "return ",
        "print(",
        "solver",
        "python",
        "json",
        "deque",
        "append(",
        "known",
        "unknown",
        "equation",
        "placeholder",
        "harmful word",
        "replaced prompt",
        "new replaced prompt",
        "rating:",
        "step ",
        "task:",
        "output format",
    )
    kept_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if any(token in lowered for token in code_tokens):
            continue
        code_symbol_count = sum(line.count(symbol) for symbol in "{}[]();=<>`_|")
        ascii_count = len(re.findall(r"[A-Za-z]", line))
        if code_symbol_count >= 3 and ascii_count >= 5:
            continue
        if re.match(r"^[#/*{}\[\],.:;\\|_\-\s]+$", line):
            continue
        kept_lines.append(line)
    return " ".join(kept_lines)


def malicious_query_language_warning(text: str, language: Any) -> str | None:
    normalized = normalize_language(language)
    if not normalized:
        return None

    clean = strip_language_check_exempt_text(text)
    arabic_count = len(re.findall(r"[\u0600-\u06FF]", clean))
    thai_count = len(re.findall(r"[\u0E00-\u0E7F]", clean))
    latin_count = len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿĞğİıŞşÇçÖöÜü]", clean))
    meaningful_count = arabic_count + thai_count + latin_count
    if meaningful_count < 20:
        return None

    if normalized == "arabic":
        ratio = arabic_count / meaningful_count
        if ratio < 0.35:
            return f"Arabic ratio {ratio:.2f} is low after stripping code/placeholders"
        return None

    if normalized == "thai":
        ratio = thai_count / meaningful_count
        if ratio < 0.35:
            return f"Thai ratio {ratio:.2f} is low after stripping code/placeholders"
        return None

    if normalized == "turkish":
        non_latin_ratio = (arabic_count + thai_count) / meaningful_count
        if non_latin_ratio > 0.10:
            return f"non-Latin target-script ratio {non_latin_ratio:.2f} is high for Turkish"
        turkish_chars = len(re.findall(r"[çğıöşüÇĞİÖŞÜı]", clean))
        turkish_words = re.findall(
            r"\b("
            r"nedir|göre|için|bilgisi|bilgisini|kamuya|açık|kaynaklara|"
            r"özetler|misin|kişinin|adlı|yayımlanmış|dayanarak|söyle|"
            r"tarihi|meslek|medeni|uyruk|milliyet|eğitim|görev"
            r")\b",
            clean.lower(),
        )
        if latin_count >= 50 and turkish_chars + len(turkish_words) == 0:
            return "Turkish signal is low after stripping code/placeholders"
    return None


def maybe_check_malicious_query_language(source: dict[str, Any], malicious_query: str) -> None:
    mode = os.getenv("MALICIOUS_QUERY_LANGUAGE_CHECK", "").strip().lower()
    if mode in {"", "0", "false", "off", "no"}:
        return
    warning = malicious_query_language_warning(malicious_query, source.get("language", ""))
    if not warning:
        return
    message = (
        f"[LanguageCheck][{source.get('attack_method')}][id={source.get('id')}] "
        f"language={source.get('language')} {warning}"
    )
    if mode == "strict":
        raise ValueError(message)
    print(message)


def build_result_record(
    source: dict[str, Any],
    *,
    method_name: str,
    model_name: str,
    malicious_query: str,
    response: str,
) -> dict[str, Any]:
    maybe_check_malicious_query_language(source, malicious_query)
    source_method = str(source.get("attack_method") or "").strip()
    legacy_methods = METHOD_ALIASES.get(method_name, set())
    attack_method = method_name if source_method in legacy_methods else source_method or method_name
    return {
        "id": int(source["id"]),
        "category": source.get("category", "unknown"),
        "language": source.get("language", ""),
        "attack_id": int(source.get("attack_id") or 0),
        "attack_method": attack_method,
        "attack_model": model_name,
        "case": source.get("case", ""),
        "malicious_query": malicious_query,
        "response": response,
    }


AttackFunction = Callable[[dict[str, Any]], tuple[str, str] | dict[str, Any]]


def run_scheduled_cases(
    *,
    method_name: str,
    input_json: str | Path,
    model_name: str,
    attack_fn: AttackFunction,
    output_path: str | Path | None = None,
    output_root: str | Path | None = None,
    max_workers: int = 5,
    save_every: int = SAVE_EVERY,
) -> list[dict[str, Any]]:
    if not model_name:
        raise ValueError("model_name 不能为空。")

    input_path = Path(input_json)
    final_output_path = Path(output_path) if output_path else output_path_for(
        method_name, model_name, input_path, output_root
    )
    records = load_scheduled_cases(input_path, expected_attack_method=method_name)
    existing_by_id = load_existing_by_id(final_output_path)
    source_by_id = {int(item["id"]): item for item in records}
    stale_ids = [
        case_id
        for case_id, existing in existing_by_id.items()
        if case_id not in source_by_id or not existing_record_matches_source(existing, source_by_id[case_id], method_name)
    ]
    for case_id in stale_ids:
        existing_by_id.pop(case_id, None)
    if stale_ids:
        print(
            f"[{method_name}][{Path(input_path).name}] "
            f"忽略 {len(stale_ids)} 条与当前调度输入不匹配的旧断点。"
        )
    pending = [item for item in records if int(item["id"]) not in existing_by_id]

    print(
        f"[{method_name}][{Path(input_path).name}][{model_name}] "
        f"总数 {len(records)}，断点已有 {len(existing_by_id)}，待处理 {len(pending)}。"
    )

    if not pending:
        save_results_by_id(existing_by_id, final_output_path)
        print(f"[{method_name}] 所有样本已处理完毕 -> {final_output_path}")
        return [existing_by_id[i] for i in sorted(existing_by_id)]

    lock = threading.Lock()
    completed = 0
    succeeded_since_save = 0

    def process_one(item: dict[str, Any]) -> dict[str, Any] | None:
        try:
            result = attack_fn(item)
            if isinstance(result, dict):
                malicious_query = str(result.get("malicious_query", ""))
                response = result.get("response", "")
            else:
                malicious_query, response = result
            return build_result_record(
                item,
                method_name=method_name,
                model_name=model_name,
                malicious_query=str(malicious_query),
                response="" if response is None else str(response),
            )
        except Exception as exc:
            print(f"[{method_name}][id={item.get('id')}] 处理失败，保留待重试: {exc}")
            return None

    worker_count = max(1, int(max_workers))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(process_one, item) for item in pending]
        for future in as_completed(futures):
            record = future.result()
            with lock:
                completed += 1
                if record is not None:
                    existing_by_id[int(record["id"])] = record
                    succeeded_since_save += 1
                    status = "ok"
                else:
                    status = "error"

                print(
                    f"[{method_name}][{Path(input_path).name}] {status} "
                    f"{completed}/{len(pending)}，累计保存队列 {len(existing_by_id)}"
                )
                if succeeded_since_save >= save_every:
                    save_results_by_id(existing_by_id, final_output_path)
                    succeeded_since_save = 0
                    print(f"[{method_name}] 已按 {save_every} 条批量落盘 -> {final_output_path}")

    save_results_by_id(existing_by_id, final_output_path)
    print(f"[{method_name}] 完成，结果已保存 -> {final_output_path}")
    return [existing_by_id[i] for i in sorted(existing_by_id)]


def current_model_from_env() -> str | None:
    return os.getenv("CURRENT_MODEL") or os.getenv("MODEL_NAME")
