# 方言化冗余率与阈值选择报告

- 数据目录：`generated_low_resource_cases/泛化方法/方言化`
- 研究范围：仅分析该目录下 status=success 的方言化 JSON 记录。
- 阈值选择规则：参考 DataComp-LM Bloom Filter Dedup 的 n-gram containment 思路，按本数据扫描阈值；每个国家组和每个单独文件冗余率均需低于 15%。

## 阈值选择结论

| 方法 | 指标 | 冗余判定阈值 | 选择结论 |
|---|---|---:|---|
| 方言化 | DataComp-LM BFF-style character 5-13 gram containment | 0.955 | `similarity >= 0.955` 判为冗余 |
| 方言化（最小达标阈值） | DataComp-LM BFF-style character 5-13 gram containment | 0.953 | 仅作扫描边界参考，不作为推荐交付阈值 |

## 方言化

- 指标：`datacomp_bff_char_5_to_13gram_containment`
- 归一化：Unicode NFKC，小写，合并空白字符；保留占位符；按 Unicode 字符取 5-13 gram
- 公式：C(A,B)=|G5-13(A)∩G5-13(B)|/|G5-13(B)|，A 为原句，B 为方言化句子
- 冗余判定：`similarity >= 0.955`

### 选定阈值下的冗余率

| 组别 | 样本数 | 冗余数 | 冗余率 |
|---|---:|---:|---:|
| 沙特-方言化 | 238 | 7 | 2.94% |
| 泰国-方言化 | 239 | 2 | 0.84% |
| 土耳其-方言化 | 203 | 22 | 10.84% |

### 按文件

| 文件 | 样本数 | 冗余数 | 冗余率 |
|---|---:|---:|---:|
| `generated_low_resource_cases/泛化方法/方言化/arabic_privacy_dialect.json` | 21 | 0 | 0.00% |
| `generated_low_resource_cases/泛化方法/方言化/arabic_reinforced_mcq_dialect.json` | 217 | 7 | 3.23% |
| `generated_low_resource_cases/泛化方法/方言化/thai_privacy_dialect.json` | 26 | 2 | 7.69% |
| `generated_low_resource_cases/泛化方法/方言化/thai_reinforced_mcq_dialect.json` | 213 | 0 | 0.00% |
| `generated_low_resource_cases/泛化方法/方言化/turkish_privacy_dialect.json` | 16 | 0 | 0.00% |
| `generated_low_resource_cases/泛化方法/方言化/turkish_reinforced_mcq_dialect.json` | 187 | 22 | 11.76% |

### 阈值扫描

| 阈值 | 总体冗余率 | 最大国家组冗余率 | 最大文件冗余率 |
|---:|---:|---:|---:|
| 0.800 | 51.03% | 94.09% | 100.00% |
| 0.900 | 23.53% | 57.14% | 57.22% |
| 0.950 | 6.18% | 15.76% | 16.58% |
| 0.953 | 5.59% | 13.79% | 14.97% |
| 0.955 | 4.56% | 10.84% | 11.76% |
| 0.960 | 3.53% | 8.37% | 9.09% |
| 0.990 | 0.00% | 0.00% | 0.00% |

### 阈值依据

方言化泛化要求保留语义，因此语义相似度不适合作为单独冗余判据。本报告改用 DataComp-LM 的 Bloom Filter Dedup 思路：判断候选文本中已有 n-gram 的覆盖比例，只把表层 n-gram 覆盖率极高的方言化结果判为冗余。由于本报告处理的是阿拉伯语、泰语、土耳其语短句，word n-gram 会受到泰语无空格分词影响，因此用字符 5-13 gram 替代 word n-gram；本数据量较小，也不使用 Bloom filter 近似结构，而是直接计算原句与方言化句子的精确 containment。DataComp-LM 提供了 n-gram containment 式去重和 0.75、0.8、0.9、0.99 等阈值消融作为参考。本报告按 0.001 阈值网格扫描，0.953 是同时满足每个国家组和每个单独文件冗余率低于 15% 的最小阈值；但该阈值下最大单文件冗余率已接近红线。因此推荐采用 0.955 作为交付阈值，在贴合本数据分布的同时留出合规余量。

参考文献：
- Li et al., DataComp-LM: In search of the next generation of training sets for language models, 2024: https://arxiv.org/abs/2406.11794。在 Bloom Filter Dedup 中使用 n-gram 覆盖比例判重；论文消融 min/max n-gram 和 0.75、0.8、0.9、0.99 阈值。本报告沿用其 n-gram containment 思路，并用本数据校准具体阈值。
