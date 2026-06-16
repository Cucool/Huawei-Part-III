# 小语种泛化冗余率与阈值选择报告

- 数据目录：`generated_low_resource_cases`
- 阈值选择规则：按方法分别扫描阈值；每个国家组和每个单独文件冗余率均需低于 `15%`。

## 阈值选择结论

| 方法 | 指标 | 冗余判定阈值 | 选择结论 |
|---|---|---:|---|
| 方言化 | 字符 3-gram Jaccard | 0.988 | `similarity >= 0.988` 判为冗余 |
| 本地模型改写 | chrF2 字符 1-6 gram F-score | 0.854 | `similarity >= 0.854` 判为冗余 |

## 方言化

- 指标：`character_3gram_jaccard`
- 归一化：Unicode NFKC，小写，合并空白字符；保留占位符
- 公式：J(A,B)=|A∩B|/|A∪B|，A/B 为字符 3-gram 集合
- 冗余判定：`similarity >= 0.988`

### 选定阈值下的冗余率

| 组别 | 样本数 | 冗余数 | 冗余率 |
|---|---:|---:|---:|
| 沙特-方言化 | 51 | 0 | 0.00% |
| 泰国-方言化 | 56 | 1 | 1.79% |
| 土耳其-方言化 | 46 | 3 | 6.52% |

### 按文件

| 文件 | 样本数 | 冗余数 | 冗余率 |
|---|---:|---:|---:|
| `generated_low_resource_cases/arabic_privacy_dialect.json` | 21 | 0 | 0.00% |
| `generated_low_resource_cases/arabic_safety_dialect_generalization.json` | 30 | 0 | 0.00% |
| `generated_low_resource_cases/thai_privacy_dialect.json` | 26 | 0 | 0.00% |
| `generated_low_resource_cases/thai_safety_dialect_generalization.json` | 30 | 1 | 3.33% |
| `generated_low_resource_cases/turkish_privacy_dialect.json` | 16 | 0 | 0.00% |
| `generated_low_resource_cases/turkish_safety_dialect_generalization.json` | 30 | 3 | 10.00% |

### 阈值扫描

| 阈值 | 总体冗余率 | 最大国家组冗余率 | 最大文件冗余率 |
|---:|---:|---:|---:|
| 0.950 | 34.64% | 56.52% | 83.33% |
| 0.960 | 24.18% | 39.13% | 60.00% |
| 0.970 | 18.30% | 30.43% | 46.67% |
| 0.980 | 10.46% | 23.91% | 36.67% |
| 0.986 | 4.58% | 13.04% | 20.00% |
| 0.987 | 3.92% | 10.87% | 16.67% |
| 0.988 | 2.61% | 6.52% | 10.00% |
| 0.989 | 1.96% | 4.35% | 6.67% |
| 0.990 | 1.31% | 4.35% | 6.67% |

### 阈值依据

方言化泛化主要通过少量字母、音系或词尾替换实现，冗余风险是只改动了极少字符。因此用字符 3-gram Jaccard 直接衡量原句与方言化句子的表层重叠度；分词对阿拉伯语、泰语、土耳其语并不一致，字符级 n-gram 更稳。扫描阈值后，0.988 是同时满足每个国家组和每个单独文件冗余率低于 15% 的最小阈值。

参考文献：
- Broder, On the Resemblance and Containment of Documents, 1997: https://doi.org/10.1109/SEQUEN.1997.666900。提出用 shingled sets/Jaccard resemblance 做文档近重复检测，是本类表层近重复判定的基础。
- Lee et al., Deduplicating Training Data Makes Language Models Better, ACL 2022: https://arxiv.org/abs/2107.06499。在语言模型训练数据去重中使用精确匹配和 MinHash 近似匹配，近重复以高 n-gram/Jaccard 重叠为核心信号。

## 本地模型改写

- 指标：`chrf2_char_1_to_6`
- 归一化：Unicode NFKC，小写，合并空白字符；计算前移除 【sb】/【sth】 等占位符
- 公式：chrFβ=(1+β²)PR/(β²P+R)，β=2，P/R 为字符 1-6 gram 平均精确率/召回率
- 冗余判定：`similarity >= 0.854`

### 选定阈值下的冗余率

| 组别 | 样本数 | 冗余数 | 冗余率 |
|---|---:|---:|---:|
| 沙特-本地模型改写 | 10 | 0 | 0.00% |
| 泰国-本地模型改写 | 10 | 0 | 0.00% |
| 土耳其-本地模型改写 | 10 | 1 | 10.00% |

### 按文件

| 文件 | 样本数 | 冗余数 | 冗余率 |
|---|---:|---:|---:|
| `generated_low_resource_cases/arabic_privacy_qwen_rewrite.json` | 10 | 0 | 0.00% |
| `generated_low_resource_cases/thai_privacy_qwen_rewrite.json` | 10 | 0 | 0.00% |
| `generated_low_resource_cases/turkish_privacy_qwen_rewrite.json` | 10 | 1 | 10.00% |

### 阈值扫描

| 阈值 | 总体冗余率 | 最大国家组冗余率 | 最大文件冗余率 |
|---:|---:|---:|---:|
| 0.700 | 16.67% | 40.00% | 40.00% |
| 0.750 | 10.00% | 30.00% | 30.00% |
| 0.800 | 10.00% | 30.00% | 30.00% |
| 0.850 | 6.67% | 20.00% | 20.00% |
| 0.854 | 3.33% | 10.00% | 10.00% |
| 0.860 | 3.33% | 10.00% | 10.00% |
| 0.900 | 3.33% | 10.00% | 10.00% |
| 0.950 | 0.00% | 0.00% | 0.00% |

### 阈值依据

大模型改写的目标是语义保持但表达变化，因此不能把“语义相似”本身当作冗余。冗余应看改写句是否大量复用原句表述。chrF 用字符 n-gram 的精确率和召回率衡量表层重合，比单纯 Jaccard 更适合长短略有变化的生成式改写；移除占位符是为了避免固定槽位抬高相似度。扫描阈值后，0.854 是同时满足每个国家组和每个单独文件冗余率低于 15% 的最小阈值。

参考文献：
- Popović, chrF: character n-gram F-score for automatic MT evaluation, WMT 2015: https://aclanthology.org/W15-3049/。提出 chrF，用字符 n-gram F-score 评价生成文本与参考文本的重合度，适合跨语言且不依赖分词。
- Papineni et al., BLEU: a Method for Automatic Evaluation of Machine Translation, ACL 2002: https://aclanthology.org/P02-1040/。BLEU 将 n-gram 重合用于自动评价生成文本，是用表层重合衡量生成输出接近程度的经典依据。
- Zhu et al., Texygen: A Benchmarking Platform for Text Generation Models, 2018: https://arxiv.org/abs/1802.01886。在文本生成评测中区分 quality、diversity、consistency，支持把生成多样性与内容一致性分开看待。
