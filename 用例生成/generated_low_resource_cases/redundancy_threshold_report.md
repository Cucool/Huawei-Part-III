# 本地模型改写冗余率与阈值选择报告

- 数据目录：`generated_low_resource_cases/泛化方法/本地模型改写`
- 研究范围：仅分析该目录下 status=success 的本地模型改写 JSON 记录。
- 阈值选择规则：采用 FineWeb 2024 的固定近重复阈值，并验证每个国家组和每个单独文件冗余率均低于 15%。

## 阈值选择结论

| 方法 | 指标 | 冗余判定阈值 | 选择结论 |
|---|---|---:|---|
| 本地模型改写 | FineWeb-style word 5-gram Jaccard | 0.750 | `similarity >= 0.750` 判为冗余 |

## 本地模型改写

- 指标：`fineweb_word_5gram_jaccard`
- 归一化：Unicode NFKC，小写，合并空白字符；计算前移除 【sb】/【sth】 等占位符；按 Unicode 词元取 word 5-gram
- 公式：J(A,B)=|A∩B|/|A∪B|，A/B 为原句与改写句的 word 5-gram 集合
- 冗余判定：`similarity >= 0.750`

### 选定阈值下的冗余率

| 组别 | 样本数 | 冗余数 | 冗余率 |
|---|---:|---:|---:|
| 沙特-本地模型改写 | 227 | 16 | 7.05% |
| 泰国-本地模型改写 | 223 | 9 | 4.04% |
| 土耳其-本地模型改写 | 199 | 0 | 0.00% |

### 按文件

| 文件 | 样本数 | 冗余数 | 冗余率 |
|---|---:|---:|---:|
| `generated_low_resource_cases/泛化方法/本地模型改写/arabic_privacy_qwen_rewrite.json` | 10 | 0 | 0.00% |
| `generated_low_resource_cases/泛化方法/本地模型改写/arabic_reinforced_mcq_qwen_rewrite.json` | 217 | 16 | 7.37% |
| `generated_low_resource_cases/泛化方法/本地模型改写/thai_privacy_qwen_rewrite.json` | 10 | 0 | 0.00% |
| `generated_low_resource_cases/泛化方法/本地模型改写/thai_reinforced_mcq_qwen_rewrite.json` | 213 | 9 | 4.23% |
| `generated_low_resource_cases/泛化方法/本地模型改写/turkish_privacy_qwen_rewrite.json` | 10 | 0 | 0.00% |
| `generated_low_resource_cases/泛化方法/本地模型改写/turkish_reinforced_mcq_qwen_rewrite.json` | 189 | 0 | 0.00% |

### 阈值扫描

| 阈值 | 总体冗余率 | 最大国家组冗余率 | 最大文件冗余率 |
|---:|---:|---:|---:|
| 0.500 | 18.03% | 27.75% | 29.03% |
| 0.600 | 12.63% | 19.82% | 20.74% |
| 0.700 | 6.78% | 11.89% | 12.44% |
| 0.750 | 3.85% | 7.05% | 7.37% |
| 0.800 | 2.47% | 5.29% | 5.53% |
| 0.900 | 0.15% | 0.45% | 0.47% |
| 0.950 | 0.15% | 0.45% | 0.47% |
| 0.980 | 0.15% | 0.45% | 0.47% |
| 0.990 | 0.15% | 0.45% | 0.47% |
| 1.000 | 0.15% | 0.45% | 0.47% |

### 阈值依据

大模型改写的目标是保留语义但改变表达，因此语义相似度不适合作为单独的冗余判据，否则有效改写也会被判成冗余。FineWeb 2024 在 LLM 预训练语料构建中使用 word 5-gram MinHash，目标是识别至少约 75% 相似的文档；本报告数据量较小，因此直接计算精确 word 5-gram Jaccard，并固定使用 FineWeb 的 0.75 近重复阈值。该指标只惩罚表层表达高度重合的改写，更符合本地模型改写的去重目标。本报告直接采用文献中的 0.750 作为冗余判定阈值，并验证该阈值下每个国家组和每个单独文件冗余率均低于 15%。

参考文献：
- Penedo et al., The FineWeb Datasets: Decanting the Web for the Finest Text Data at Scale, 2024: https://arxiv.org/abs/2406.17557。在 web-scale LLM 预训练语料构建中使用 word 5-gram MinHash 去重，参数目标是识别至少约 75% 相似的文档。
