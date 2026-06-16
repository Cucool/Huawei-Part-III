import pandas as pd
import json
import re
from openai import OpenAI
import os

# ==================================================
# OpenAI Compatible Client
# ==================================================
client = OpenAI(
    api_key="sk-cc9d3633ace642ec8d3d71f3d95e6c08",
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
)
MODEL = "qwen3.6-flash"


# ==================================================
# 翻译函数
# ==================================================

def translate_to_chinese(text):

    if not text.strip():
        return ""

    current_system_prompt = """
你是专业的阿拉伯语翻译助手。

要求：
1. 将阿拉伯语翻译为简体中文
2. 只输出中文
3. 不要解释
4. 不要添加额外内容
"""

    prompt = f"""
请翻译下面阿拉伯语：

{text}
"""

    try:

        completion = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": current_system_prompt
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.1,
            max_tokens=512
        )

        content = completion.choices[0].message.content.strip()

        return content

    except Exception as e:

        print("翻译失败:", e)

        return ""


# ==================================================
# 阿拉伯语方言规则（MSA → 方言）
# ==================================================

ARABIC_RULES = [

    # ==================================================
    # 辅音规则
    # ==================================================

    {
        "id": "AR1",
        "description": "海湾方言 ق → گ",
        "pattern": r"ق",
        "replace": "گ"
    },

    {
        "id": "AR2",
        "description": "埃及方言 ق → أ",
        "pattern": r"ق",
        "replace": "أ"
    },

    {
        "id": "AR3",
        "description": "ث → ت",
        "pattern": r"ث",
        "replace": "ت"
    },

    {
        "id": "AR4",
        "description": "ذ → د",
        "pattern": r"ذ",
        "replace": "د"
    },

    {
        "id": "AR5",
        "description": "ظ → ض",
        "pattern": r"ظ",
        "replace": "ض"
    },

    # ==================================================
    # 元音 / 发音变化
    # ==================================================

    {
        "id": "AR6",
        "description": "双元音单化 بيت → بت",
        "pattern": r"بيت",
        "replace": "بت"
    },

    {
        "id": "AR7",
        "description": "anta → inta",
        "pattern": r"أنت",
        "replace": "إنت"
    },

    {
        "id": "AR8",
        "description": "定冠词 al → il",
        "pattern": r"\bال",
        "replace": "إل"
    },

    # ==================================================
    # 北非语法
    # ==================================================

    {
        "id": "AR9",
        "description": "第一人称前缀 n-",
        "pattern": r"أكتب",
        "replace": "نكتب"
    },

    # ==================================================
    # 新增规则（第10条）
    # ==================================================

    {
        "id": "AR10",
        "description": "黎凡特方言 كيف → كيف/إزي",
        "pattern": r"كيف",
        "replace": "إزي"
    }
]


# ==================================================
# 单规则转换
# 阿拉伯语不做多规则叠加
# ==================================================

def transform_sentence(sentence):

    outputs = []

    for rule in ARABIC_RULES:

        transformed = re.sub(
            rule["pattern"],
            rule["replace"],
            sentence
        )

        if transformed != sentence:

            outputs.append({
                "rule_id": rule["id"],
                "description": rule["description"],
                "transformed": transformed
            })

    return outputs


# ==================================================
# Excel 处理
# ==================================================

def process_excel(input_file, output_json):

    # 多级表头
    df = pd.read_excel(input_file, header=[0, 1])

    results = []

    for col in df.columns:

        main_col = str(col[0]).strip()
        sub_col = str(col[1]).strip()

        # 只处理阿拉伯语列
        if main_col != "沙特":
            continue

        # 每个子列只处理前4条
        for idx, value in enumerate(df[col].head(2)):

            if pd.isna(value):
                continue

            sentence = str(value).strip()

            if not sentence:
                continue

            # 原句翻译
            original_zh = translate_to_chinese(sentence)

            # 每条规则单独生成
            transformed_outputs = transform_sentence(sentence)

            for item in transformed_outputs:

                transformed = item["transformed"]

                # 翻译方言句
                transformed_zh = translate_to_chinese(transformed)

                results.append({

                    "row": idx + 1,

                    "category": sub_col,

                    "original": sentence,
                    "original_zh": original_zh,

                    "transformed": transformed,
                    "transformed_zh": transformed_zh,

                    "rule_id": item["rule_id"],
                    "rule_description": item["description"]
                })

                print(
                    f"已处理: "
                    f"{sentence} -> {transformed}"
                )

    # 保存 JSON
    with open(
        output_json,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            results,
            f,
            ensure_ascii=False,
            indent=2
        )

    print(f"\n处理完成，共 {len(results)} 条")
    print(f"输出文件: {output_json}")


# ==================================================
# 主程序
# ==================================================

if __name__ == "__main__":

    process_excel(
        input_file="table.csv",
        output_json="arabic_output.json"
    )