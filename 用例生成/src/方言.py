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
你是专业的土耳其语翻译助手。
要求：
1. 将土耳其语翻译为简体中文
2. 只输出中文
3. 不要解释
4. 不要添加额外内容
"""

    prompt = f"""
请翻译下面土耳其语：

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
# 泰语方言转换规则
# ==================================================

THAILAND_RULES = [
    {
        "id": "T1",
        "description": "ร → ฮ（北部/东北方言）",
        "pattern": r"ร",
        "replace": "ฮ"
    },
    {
        "id": "T2-1",
        "description": "复辅音 pl- → p-",
        "pattern": r"ปลา",
        "replace": "ปา"
    },
    {
        "id": "T2-2",
        "description": "复辅音 tr- → k-",
        "pattern": r"ตรง",
        "replace": "กง"
    },
    {
        "id": "T3-1",
        "description": "พ → ป",
        "pattern": r"พ",
        "replace": "ป"
    },
    {
        "id": "T3-2",
        "description": "ท → ต",
        "pattern": r"ท",
        "replace": "ต"
    },
    {
        "id": "T3-3",
        "description": "ช → จ",
        "pattern": r"ช",
        "replace": "จ"
    },
    {
        "id": "T3-4",
        "description": "ค → ก",
        "pattern": r"ค",
        "replace": "ก"
    },
    {
        "id": "T4",
        "description": "จริง → จิง",
        "pattern": r"จริง",
        "replace": "จิง"
    },
    {
        "id": "T5",
        "description": "เนื้อ → เนี้ย",
        "pattern": r"เนื้อ",
        "replace": "เนี้ย"
    },
    {
        "id": "T6",
        "description": "ตัว → โต",
        "pattern": r"ตัว",
        "replace": "โต"
    },
    {
        "id": "T7",
        "description": "ครึ่ง → เคิ่ง",
        "pattern": r"ครึ่ง",
        "replace": "เคิ่ง"
    },
    {
        "id": "T8",
        "description": "ขวา → ขัว",
        "pattern": r"ขวา",
        "replace": "ขัว"
    },
    {
        "id": "T9",
        "description": "เมือง → เมืองง（口语化延长）",
        "pattern": r"เมือง",
        "replace": "เมืองง"
    },
    {
        "id": "T10-1",
        "description": "เที่ยว → เถียว",
        "pattern": r"เที่ยว",
        "replace": "เถียว"
    },
    {
        "id": "T10-2",
        "description": "พ่อ → ผอ",
        "pattern": r"พ่อ",
        "replace": "ผอ"
    }
]

TURKISH_RULES = [
    # ==================================================
    # 辅音规则
    # ==================================================

    {
        "id": "TR1",
        "description": "黑海方言 k → g",
        "pattern": r"\bk",
        "replace": "g"
    },

    {
        "id": "TR2-1",
        "description": "t → d",
        "pattern": r"\bt",
        "replace": "d"
    },

    {
        "id": "TR2-2",
        "description": "p → b",
        "pattern": r"\bp",
        "replace": "b"
    },

    {
        "id": "TR3",
        "description": "东部方言 k → q",
        "pattern": r"\bk",
        "replace": "q"
    },

    {
        "id": "TR4",
        "description": "yor → yo",
        "pattern": r"yor\b",
        "replace": "yo"
    },

    # ==================================================
    # 元音规则
    # ==================================================

    {
        "id": "TR5-1",
        "description": "e → a（有限元音后移）",
        "pattern": r"ben",
        "replace": "ban"
    },

    {
        "id": "TR5-2",
        "description": "i → ı（有限元音后移）",
        "pattern": r"bir",
        "replace": "bır"
    },

    {
        "id": "TR6",
        "description": "元音省略",
        "pattern": r"burada",
        "replace": "burda"
    },

    {
        "id": "TR7",
        "description": "长元音化",
        "pattern": r"dağ",
        "replace": "daa"
    },

    {
        "id": "TR8",
        "description": "mı/mu 疑问小品词口语延长",
        "pattern": r"\bm[ıu]\b",
        "replace": "mıı"
    },

    # ==================================================
    # 语法规则
    # ==================================================

    {
        "id": "TR9",
        "description": "过去时 ti → di",
        "pattern": r"ti\b",
        "replace": "di"
    },

    {
        "id": "TR10",
        "description": "句尾增加语气词 la",
        "pattern": r"$",
        "replace": " la"
    }
]


# ==================================================
# 句子转换函数
# ==================================================

def transform_sentence(sentence):
    transformed = sentence
    applied_rules = []

    for rule in TURKISH_RULES:
        new_text = re.sub(
            rule["pattern"],
            rule["replace"],
            transformed
        )

        if new_text != transformed:
            applied_rules.append({
                "rule_id": rule["id"],
                "description": rule["description"]
            })
            transformed = new_text

    return transformed, applied_rules

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

        # 只处理泰国下面的子列
        if main_col != "土耳其":
            continue

        for idx, value in enumerate(df[col].head(4)):
            if pd.isna(value):
                continue
            sentence = str(value).strip()

            if not sentence:
                continue

            # 方言转换
            transformed, rules = transform_sentence(sentence)

            # 中文翻译
            original_zh = translate_to_chinese(sentence)
            transformed_zh = translate_to_chinese(transformed)
            results.append({
                "row": idx + 1,
                "category": sub_col,
                "original": sentence,
                "original_zh": original_zh,
                "transformed": transformed,
                "transformed_zh": transformed_zh,
                "applied_rules": rules
            })
            print(f"已处理: {sentence}")

    # 保存 JSON
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(results,f,ensure_ascii=False,indent=2)
    print(f"\n处理完成，共 {len(results)} 条")
    print(f"输出文件: {output_json}")

# ==================================================
# 主程序
# ==================================================

if __name__ == "__main__":
    process_excel(
        input_file="table.csv",
        output_json="turkey_output.json"
    )