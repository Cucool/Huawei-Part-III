from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from Attacker import Attacker

JAILBREAK_DIR = Path(__file__).resolve().parents[1]
if str(JAILBREAK_DIR) not in sys.path:
    sys.path.insert(0, str(JAILBREAK_DIR))

from runner_utils import current_model_from_env, run_scheduled_cases


METHOD_NAME = "Trojfill"
N = 2
TRANSFORM_TYPE = "Caesar"


def build_attack_fn(model_name: str):
    attacker = Attacker(
        attacker_model_id=model_name,
        target_model_id=model_name,
    )

    def attack_one(item: dict[str, Any]) -> dict[str, str]:
        _score, attack_question, response = attacker.jailbreak_prompt(
            item["case"],
            N=N,
            transform_type=TRANSFORM_TYPE,
            language=item.get("language", ""),
        )
        return {
            "malicious_query": attack_question,
            "response": response,
        }

    return attack_one


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trojfill scheduled runner")
    parser.add_argument("--model_name", default=current_model_from_env())
    parser.add_argument("--input_json", type=Path, default=os.getenv("ATTACK_INPUT_JSON"))
    parser.add_argument("--output_root", type=Path, default=os.getenv("ATTACK_OUTPUT_ROOT"))
    parser.add_argument("--max_workers", type=int, default=5)
    return parser.parse_args()


def main() -> None:
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
