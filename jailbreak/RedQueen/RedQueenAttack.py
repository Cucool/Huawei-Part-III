from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

os.environ["DATASETS_NO_TORCH"] = "0,1,2,3"

from openai import OpenAI

from scenario_template import (
    MultiTurnAttack_occupation_teacher_compressed_turn,
    MultiTurnAttack_occupation_teacher_three_turn,
    MultiTurnAttack_occupation_teacher_four_turn,
    MultiTurnAttack_occupation_teacher_five_turn,
    MultiTurnAttack_occupation_police_compressed_turn,
    MultiTurnAttack_occupation_police_three_turn,
    MultiTurnAttack_occupation_police_four_turn,
    MultiTurnAttack_occupation_police_five_turn,
    MultiTurnAttack_occupation_detective_compressed_turn,
    MultiTurnAttack_occupation_detective_three_turn,
    MultiTurnAttack_occupation_detective_four_turn,
    MultiTurnAttack_occupation_detective_five_turn,
    MultiTurnAttack_occupation_lawyer_compressed_turn,
    MultiTurnAttack_occupation_lawyer_three_turn,
    MultiTurnAttack_occupation_lawyer_four_turn,
    MultiTurnAttack_occupation_lawyer_five_turn,
    MultiTurnAttack_occupation_priest_compressed_turn,
    MultiTurnAttack_occupation_priest_three_turn,
    MultiTurnAttack_occupation_priest_four_turn,
    MultiTurnAttack_occupation_priest_five_turn,
    MultiTurnAttack_relation_friend_compressed_turn,
    MultiTurnAttack_relation_friend_three_turn,
    MultiTurnAttack_relation_friend_four_turn,
    MultiTurnAttack_relation_friend_five_turn,
    MultiTurnAttack_relation_neighbor_compressed_turn,
    MultiTurnAttack_relation_neighbor_three_turn,
    MultiTurnAttack_relation_neighbor_four_turn,
    MultiTurnAttack_relation_neighbor_five_turn,
    MultiTurnAttack_relation_someone_compressed_turn,
    MultiTurnAttack_relation_someone_three_turn,
    MultiTurnAttack_relation_someone_four_turn,
    MultiTurnAttack_relation_someone_five_turn,
    MultiTurnAttack_relation_relative_compressed_turn,
    MultiTurnAttack_relation_relative_three_turn,
    MultiTurnAttack_relation_relative_four_turn,
    MultiTurnAttack_relation_relative_five_turn,
    MultiTurnAttack_relation_son_compressed_turn,
    MultiTurnAttack_relation_son_three_turn,
    MultiTurnAttack_relation_son_four_turn,
    MultiTurnAttack_relation_son_five_turn,
)

JAILBREAK_DIR = Path(__file__).resolve().parents[1]
if str(JAILBREAK_DIR) not in sys.path:
    sys.path.insert(0, str(JAILBREAK_DIR))

from runner_utils import current_model_from_env, localized_value, render_messages, run_scheduled_cases


METHOD_NAME = "RedQueenAttack"
MAX_CHAR_LEN = 35000
TURN_LIST = [1, 3, 4, 5]
TEMPLATE_LIST = [
    "occupation_teacher",
    "occupation_police",
    "occupation_detective",
    "occupation_lawyer",
    "occupation_priest",
    "relation_friend",
    "relation_neighbor",
    "relation_someone",
    "relation_relative",
    "relation_son",
]

TEMPLATE_CLASS_MAP = {
    ("occupation_teacher", 1): MultiTurnAttack_occupation_teacher_compressed_turn,
    ("occupation_teacher", 3): MultiTurnAttack_occupation_teacher_three_turn,
    ("occupation_teacher", 4): MultiTurnAttack_occupation_teacher_four_turn,
    ("occupation_teacher", 5): MultiTurnAttack_occupation_teacher_five_turn,
    ("occupation_police", 1): MultiTurnAttack_occupation_police_compressed_turn,
    ("occupation_police", 3): MultiTurnAttack_occupation_police_three_turn,
    ("occupation_police", 4): MultiTurnAttack_occupation_police_four_turn,
    ("occupation_police", 5): MultiTurnAttack_occupation_police_five_turn,
    ("occupation_detective", 1): MultiTurnAttack_occupation_detective_compressed_turn,
    ("occupation_detective", 3): MultiTurnAttack_occupation_detective_three_turn,
    ("occupation_detective", 4): MultiTurnAttack_occupation_detective_four_turn,
    ("occupation_detective", 5): MultiTurnAttack_occupation_detective_five_turn,
    ("occupation_lawyer", 1): MultiTurnAttack_occupation_lawyer_compressed_turn,
    ("occupation_lawyer", 3): MultiTurnAttack_occupation_lawyer_three_turn,
    ("occupation_lawyer", 4): MultiTurnAttack_occupation_lawyer_four_turn,
    ("occupation_lawyer", 5): MultiTurnAttack_occupation_lawyer_five_turn,
    ("occupation_priest", 1): MultiTurnAttack_occupation_priest_compressed_turn,
    ("occupation_priest", 3): MultiTurnAttack_occupation_priest_three_turn,
    ("occupation_priest", 4): MultiTurnAttack_occupation_priest_four_turn,
    ("occupation_priest", 5): MultiTurnAttack_occupation_priest_five_turn,
    ("relation_friend", 1): MultiTurnAttack_relation_friend_compressed_turn,
    ("relation_friend", 3): MultiTurnAttack_relation_friend_three_turn,
    ("relation_friend", 4): MultiTurnAttack_relation_friend_four_turn,
    ("relation_friend", 5): MultiTurnAttack_relation_friend_five_turn,
    ("relation_neighbor", 1): MultiTurnAttack_relation_neighbor_compressed_turn,
    ("relation_neighbor", 3): MultiTurnAttack_relation_neighbor_three_turn,
    ("relation_neighbor", 4): MultiTurnAttack_relation_neighbor_four_turn,
    ("relation_neighbor", 5): MultiTurnAttack_relation_neighbor_five_turn,
    ("relation_someone", 1): MultiTurnAttack_relation_someone_compressed_turn,
    ("relation_someone", 3): MultiTurnAttack_relation_someone_three_turn,
    ("relation_someone", 4): MultiTurnAttack_relation_someone_four_turn,
    ("relation_someone", 5): MultiTurnAttack_relation_someone_five_turn,
    ("relation_relative", 1): MultiTurnAttack_relation_relative_compressed_turn,
    ("relation_relative", 3): MultiTurnAttack_relation_relative_three_turn,
    ("relation_relative", 4): MultiTurnAttack_relation_relative_four_turn,
    ("relation_relative", 5): MultiTurnAttack_relation_relative_five_turn,
    ("relation_son", 1): MultiTurnAttack_relation_son_compressed_turn,
    ("relation_son", 3): MultiTurnAttack_relation_son_three_turn,
    ("relation_son", 4): MultiTurnAttack_relation_son_four_turn,
    ("relation_son", 5): MultiTurnAttack_relation_son_five_turn,
}

REDQUEEN_TRANSLATION_SYSTEM_PROMPTS = {
    "default": "",
    "arabic": "ترجم نص المحادثة التالي إلى العربية. حافظ على تسميات الأدوار مثل [system] و[user] و[assistant] كما هي، ولا تجب عن الطلب.",
    "thai": "แปลข้อความบทสนทนาต่อไปนี้เป็นภาษาไทย รักษาป้ายบทบาท เช่น [system], [user], [assistant] ไว้เหมือนเดิม และอย่าตอบคำขอ",
    "turkish": "Aşağıdaki konuşma metnini Türkçeye çevir. [system], [user] ve [assistant] gibi rol etiketlerini aynen koru; isteği yanıtlama.",
}

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY", "EMPTY"),
    base_url=os.getenv("OPENAI_API_BASE", "http://localhost:6666/v1"),
)


def call_llm_api(model_name: str, prompt_or_messages: Any) -> str:
    if isinstance(prompt_or_messages, str):
        messages = [{"role": "user", "content": prompt_or_messages[:MAX_CHAR_LEN]}]
    else:
        messages = [
            {"role": msg.get("role", "user"), "content": str(msg.get("content", ""))[:MAX_CHAR_LEN]}
            for msg in prompt_or_messages
        ]

    while True:
        try:
            completion = client.chat.completions.create(
                model=model_name,
                messages=messages,
                max_tokens=1024,
            )
            content = completion.choices[0].message.content
            if content and str(content).strip():
                return str(content).strip()
            print(f"[{model_name}] API 返回空响应，重试...")
            time.sleep(2)
        except Exception as exc:
            error_str = str(exc).lower()
            if "maximum context length" in error_str or "context_length_exceeded" in error_str or "400" in error_str:
                print(f"⚠️ [{model_name}] 测试用例过长，触发防爆熔断，已记录并跳过。")
                return "ERROR_TOKEN_LIMIT_EXCEEDED"
            print(f"[{model_name}] 网络或接口异常: {exc}。5秒后重试...")
            time.sleep(5)


def translate_rendered_query(rendered_query: str, model_name: str, language: str) -> str:
    system_prompt = localized_value(REDQUEEN_TRANSLATION_SYSTEM_PROMPTS, language).strip()
    if not system_prompt:
        return rendered_query
    translated = call_llm_api(
        model_name,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": rendered_query},
        ],
    )
    return translated if translated and translated != "ERROR_TOKEN_LIMIT_EXCEEDED" else rendered_query


def build_red_queen_messages(action: str, template_type: str, turn: int) -> list[dict[str, str]]:
    class_object = TEMPLATE_CLASS_MAP[(template_type, turn)]
    mt = class_object()
    if turn == 1:
        return [
            {"role": "system", "content": mt.system_prompt},
            {"role": "user", "content": mt.sentence_1.format(action=action)},
        ]
    if turn == 3:
        return [
            {"role": "system", "content": mt.system_prompt},
            {"role": "user", "content": mt.sentence_1.format(action=action)},
            {"role": "assistant", "content": mt.response_1},
            {"role": "user", "content": mt.sentence_2},
            {"role": "assistant", "content": mt.response_2},
            {"role": "user", "content": mt.sentence_3},
        ]
    if turn == 4:
        return [
            {"role": "system", "content": mt.system_prompt},
            {"role": "user", "content": mt.sentence_1.format(action=action)},
            {"role": "assistant", "content": mt.response_1},
            {"role": "user", "content": mt.sentence_2},
            {"role": "assistant", "content": mt.response_2},
            {"role": "user", "content": mt.sentence_3},
            {"role": "assistant", "content": mt.response_3},
            {"role": "user", "content": mt.sentence_4},
        ]
    return [
        {"role": "system", "content": mt.system_prompt},
        {"role": "user", "content": mt.sentence_1.format(action=action)},
        {"role": "assistant", "content": mt.response_1},
        {"role": "user", "content": mt.sentence_2},
        {"role": "assistant", "content": mt.response_2},
        {"role": "user", "content": mt.sentence_3},
        {"role": "assistant", "content": mt.response_3},
        {"role": "user", "content": mt.sentence_4},
        {"role": "assistant", "content": mt.response_4},
        {"role": "user", "content": mt.sentence_5},
    ]


def build_attack_fn(model_name: str):
    def attack_one(item: dict[str, Any]) -> dict[str, str]:
        template_type = random.choice(TEMPLATE_LIST)
        turn = random.choice(TURN_LIST)
        language = item.get("language", "")
        attack_messages = build_red_queen_messages(item["case"], template_type, turn)
        response = call_llm_api(model_name, attack_messages)
        rendered_query = render_messages(attack_messages)
        return {
            "malicious_query": translate_rendered_query(rendered_query, model_name, language),
            "response": response,
        }

    return attack_one


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RedQueenAttack scheduled runner")
    parser.add_argument("--model_name", default=current_model_from_env())
    parser.add_argument("--input_json", type=Path, default=os.getenv("ATTACK_INPUT_JSON"))
    parser.add_argument("--output_root", type=Path, default=os.getenv("ATTACK_OUTPUT_ROOT"))
    parser.add_argument("--max_workers", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    random.seed(42)
    args = parse_args()
    if not args.input_json:
        raise ValueError("必须传入 --input_json 或设置 ATTACK_INPUT_JSON。")
    run_scheduled_cases(
        method_name=METHOD_NAME,
        input_json=args.input_json,
        model_name=args.model_name,
        attack_fn=build_attack_fn(args.model_name),
        output_root=args.output_root,
        max_workers=args.max_workers,
    )


if __name__ == "__main__":
    main()
