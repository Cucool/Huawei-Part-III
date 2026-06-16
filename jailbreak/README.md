# Jailbreak 批量评测运行说明

本目录使用 `zrun_all.sh` 在 Slurm 集群上调度 `all.py`，完成越狱攻击样本分发、目标模型回复生成、统一裁判和成功率汇总。

## 入口脚本

```bash
sbatch zrun_all.sh
```

## 默认模型

当前目标模型列表：

```bash
Qwen2.5-7B
Qwen3-32B
qwen3-30b-a3b
Mistral-small-2509
```

统一裁判模型默认是：

```bash
Qwen2.5-7B
```

## 运行模式

通过 `JAILBREAK_MODE` 控制流程，默认是 `full`。

```bash
export JAILBREAK_MODE=full sbatch zrun_all.sh
```

可选模式：

| 模式 | 行为 |
| --- | --- |
| `dispatch` | 只读取 `pro_data` 并生成 `scheduled_inputs`，不启动 vLLM |
| `response` | 对每个目标模型生成攻击回复并汇总，不做统一裁判 |
| `attack` | 与 `response` 等价 |
| `judge` | 只部署裁判模型，对已有 response 做统一裁判并汇总 |
| `full` | 先跑 response，再跑 judge，最后生成成功率矩阵 |

## 推荐执行顺序

首次运行可直接：

```bash
export JAILBREAK_MODE=full sbatch zrun_all.sh
```

如果希望先只生成分发文件：

```bash
export JAILBREAK_MODE=dispatch sbatch zrun_all.sh
```

之后继续同一个 run：

```bash
export JAILBREAK_RUN_ID=<已有run_id>
export JAILBREAK_MODE=full
sbatch zrun_all.sh
```

`JAILBREAK_RUN_ID` 很重要。脚本默认用当前时间生成 run id；如果不指定，会创建新的输出目录，无法复用上一次 dispatch。

## 输出目录

每次运行输出到：

```text
runs/<JAILBREAK_RUN_ID>/
```

主要文件结构：

```text
runs/<run_id>/
  run_info.json
  scheduled_inputs/
    <category>/
      <attack_id>_<attack_method>/
        <category>.json
  attack_<AttackMethod>/
    <model_name>/
      <category>.json
  <model_name>/
    <category>.json
    <category>_judge.json
    summary.json
  jailbreak_success_rate_matrix.csv
  jailbreak_success_rate_matrix.json
```

其中：

- `scheduled_inputs/` 是 dispatch 生成的攻击方法分片。
- `attack_<AttackMethod>/<model>/<category>.json` 是单个攻击方法的输出。
- `<model>/<category>.json` 是该模型该分类的攻击结果汇总。
- `<model>/<category>_judge.json` 是统一裁判结果。
- `<model>/summary.json` 是单模型成功率汇总。
- `jailbreak_success_rate_matrix.*` 是多模型成功率矩阵。

## vLLM 部署方式

脚本会为每个目标模型单独启动 vLLM OpenAI API 服务：

```bash
python3 -m vllm.entrypoints.openai.api_server \
  --model /models \
  --served-model-name <model_name> \
  --port <port> \
  --tensor-parallel-size 4 \
  --trust-remote-code \
  --gpu-memory-utilization 0.90 \
  --max-num-seqs 256 \
  --max-model-len 30000
```

端口计算规则：

```bash
VLLM_PORT=6000 + SLURM_JOB_ID % 1000
```

随后设置：

```bash
OPENAI_API_BASE=http://localhost:<VLLM_PORT>/v1
OPENAI_API_KEY=EMPTY
```

所有攻击脚本和裁判逻辑都通过这个 OpenAI 兼容接口调用当前部署的模型。

## 续跑逻辑

脚本内置两类完成度检查：

```bash
all.py status --stage response
all.py status --stage judge
```

response 阶段：

- 如果某个目标模型的 response 已完成，则跳过该模型部署。
- 如果未完成，则启动该目标模型，执行 `all.py run --mode response`。

judge 阶段：

- 先检查所有目标模型是否都已完成裁判。
- 如果存在未完成项，则只部署一次 `JAILBREAK_JUDGE_MODEL`。
- 对未完成裁判的目标模型执行 `all.py run --mode judge`。

因此中断后可以用相同 `JAILBREAK_RUN_ID` 重新提交，脚本会尽量跳过已完成部分。

## 常用参数

可以通过环境变量调整并发和入口：

```bash
export ATTACK_MAX_WORKERS=5
export JUDGE_MAX_WORKERS=64
export JAILBREAK_PIPELINE_SCRIPT=all.py
export JAILBREAK_JUDGE_MODEL=Qwen2.5-7B
export JAILBREAK_MODE=full
export JAILBREAK_RUN_ID=20260101_120000
sbatch zrun_all.sh
```

`ATTACK_MAX_WORKERS` 会传给攻击脚本的 `--max_workers`。  
`JUDGE_MAX_WORKERS` 会传给统一裁判的 `--judge_workers`。

## 与 all.py 的关系

`zrun_all.sh` 只负责集群资源、vLLM 部署、模型轮转和续跑判断。实际数据分发、攻击脚本调度、汇总、裁判都由 `all.py` 完成。

核心调用包括：

```bash
python -u all.py run --mode dispatch ...
python -u all.py run --mode response --model_name <target_model> ...
python -u all.py run --mode judge --model_name <target_model> --judge_model <judge_model> ...
python -u all.py summarize --models <model1> <model2> ...
```

也可以直接查看攻击方法列表：

```bash
python all.py list-methods
```
