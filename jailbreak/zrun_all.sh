#!/bin/bash
#SBATCH -p gpu_ai
#SBATCH -N 1
#SBATCH -n 8
#SBATCH -G 4
#SBATCH -o run_all_%J.out
#SBATCH -w 5500-node04

set -euo pipefail

if [[ -n "${JAILBREAK_TARGET_MODELS:-}" ]]; then
    read -r -a TARGET_MODELS <<< "${JAILBREAK_TARGET_MODELS}"
else
    TARGET_MODELS=("Qwen2.5-7B" "Qwen3-32B" "qwen3-30b-a3b" "Mistral-small-2509")
fi

export ATTACK_MAX_WORKERS=${ATTACK_MAX_WORKERS:-5}
export JUDGE_MAX_WORKERS=${JUDGE_MAX_WORKERS:-64}
export JAILBREAK_MODE=${JAILBREAK_MODE:-full}
export JAILBREAK_PIPELINE_SCRIPT=${JAILBREAK_PIPELINE_SCRIPT:-all.py}
export JAILBREAK_DATA_DIR=${JAILBREAK_DATA_DIR:-pro_data}
export JAILBREAK_JUDGE_MODEL=${JAILBREAK_JUDGE_MODEL:-Qwen2.5-7B}

module load singularity/4.0.2
module load Anaconda/mini3-23.1.0
module load cuda/12.1.0
source activate /share/home/wuwb36g/hejx/miniconda3/envs/llm_env

IMAGE_FILE="/share/home/wuwb36g/projects/qwen3_deployment/images/vllm_cu121.sif"
CODE_DIR="/share/home/wuwb36g/projects/qwen3_deployment/code/code-vllm/jailbreak"
PYTHON_BIN="/share/home/wuwb36g/hejx/miniconda3/envs/llm_env/bin/python"

export SINGULARITY_TMPDIR=/tmp/$USER/singularity_tmp
export SINGULARITY_CACHEDIR=/tmp/$USER/singularity_cache
mkdir -p $SINGULARITY_TMPDIR $SINGULARITY_CACHEDIR

GPU_COUNT=4

cd ${CODE_DIR}

RUN_ID=${JAILBREAK_RUN_ID:-$(date +"%Y%m%d_%H%M%S")}
export JAILBREAK_RUN_ID="${RUN_ID}"
RUN_OUTPUT_ROOT="${CODE_DIR}/runs/${RUN_ID}"
RUN_DISPATCH_DIR="${RUN_OUTPUT_ROOT}/scheduled_inputs"
mkdir -p "${RUN_OUTPUT_ROOT}"
echo "[Run] 输出目录: ${RUN_OUTPUT_ROOT}"
echo "[Run] 运行模式: ${JAILBREAK_MODE}"
echo "[Run] 调度脚本: ${JAILBREAK_PIPELINE_SCRIPT}"
echo "[Run] 数据目录: ${JAILBREAK_DATA_DIR}"
echo "[Run] 裁判模型: ${JAILBREAK_JUDGE_MODEL}"
echo "[Run] 目标模型: ${TARGET_MODELS[*]}"

case "${JAILBREAK_MODE}" in
    dispatch|response|attack|judge|full)
        ;;
    *)
        echo "未知 JAILBREAK_MODE=${JAILBREAK_MODE}，可选: dispatch, response, attack, judge, full"
        exit 2
        ;;
esac

if [[ "${JAILBREAK_MODE}" == "dispatch" ]]; then
    "${PYTHON_BIN}" -u "${JAILBREAK_PIPELINE_SCRIPT}" \
        run \
        --data_dir "${JAILBREAK_DATA_DIR}" \
        --mode dispatch \
        --output_root "${RUN_OUTPUT_ROOT}" \
        --dispatch_dir "${RUN_DISPATCH_DIR}"

    rm -rf $SINGULARITY_TMPDIR $SINGULARITY_CACHEDIR
    exit 0
fi

BASE_PORT=6000
PORT_OFFSET=$(( ${SLURM_JOB_ID:-0} % 1000 ))
VLLM_PORT=$(( BASE_PORT + PORT_OFFSET ))

export OPENAI_API_BASE="http://localhost:${VLLM_PORT}/v1"
export OPENAI_API_KEY="EMPTY"
export no_proxy="localhost,127.0.0.1,::1"

VLLM_PID=""

cleanup_vllm() {
    if [[ -n "${VLLM_PID:-}" ]]; then
        kill -- "-${VLLM_PID}" 2>/dev/null || kill "${VLLM_PID}" 2>/dev/null || true
        sleep 10
        kill -9 -- "-${VLLM_PID}" 2>/dev/null || kill -9 "${VLLM_PID}" 2>/dev/null || true
        wait "${VLLM_PID}" 2>/dev/null || true
        VLLM_PID=""
    fi

    pkill -f "vllm.entrypoints.openai.api_server.*--port ${VLLM_PORT}" 2>/dev/null || true

    while curl -s "http://localhost:${VLLM_PORT}/v1/models" > /dev/null; do
        sleep 5
    done
}

trap cleanup_vllm EXIT

start_vllm() {
    local served_model="$1"
    local model_dir="/share/home/wuwb36g/projects/qwen3_deployment/models/${served_model}"
    cleanup_vllm

    setsid singularity exec --nv \
        --env GLOO_SOCKET_IFNAME=lo \
        --env NCCL_SOCKET_IFNAME=lo \
        --env CC=gcc \
        --bind ${model_dir}:/models \
        ${IMAGE_FILE} \
        python3 -m vllm.entrypoints.openai.api_server \
        --model /models \
        --served-model-name ${served_model} \
        --port ${VLLM_PORT} \
        --tensor-parallel-size ${GPU_COUNT} \
        --trust-remote-code \
        --gpu-memory-utilization 0.90 \
        --max-num-seqs 256 \
        --max-model-len 30000 &

    VLLM_PID=$!
    while ! curl -s http://localhost:${VLLM_PORT}/v1/models > /dev/null; do
        sleep 5
    done
}

response_complete() {
    local target_model="$1"
    "${PYTHON_BIN}" -u "${JAILBREAK_PIPELINE_SCRIPT}" \
        status \
        --stage response \
        --data_dir "${JAILBREAK_DATA_DIR}" \
        --model_name "${target_model}" \
        --output_root "${RUN_OUTPUT_ROOT}" \
        --dispatch_dir "${RUN_DISPATCH_DIR}"
}

judge_complete() {
    local target_model="$1"
    "${PYTHON_BIN}" -u "${JAILBREAK_PIPELINE_SCRIPT}" \
        status \
        --stage judge \
        --data_dir "${JAILBREAK_DATA_DIR}" \
        --model_name "${target_model}" \
        --output_root "${RUN_OUTPUT_ROOT}" \
        --dispatch_dir "${RUN_DISPATCH_DIR}"
}

run_response_for_model() {
    local target_model="$1"
    export CURRENT_MODEL="${target_model}"

    if response_complete "${target_model}"; then
        echo "[Response][${target_model}] 已完成，跳过模型部署。"
        return 0
    fi

    echo "[Response][${target_model}] 未完成，开始部署并生成回复。"
    start_vllm "${target_model}"

    PIPELINE_ARGS=(
        run
        --data_dir "${JAILBREAK_DATA_DIR}"
        --model_name "${target_model}"
        --mode response
        --output_root "${RUN_OUTPUT_ROOT}"
        --dispatch_dir "${RUN_DISPATCH_DIR}"
        --max_workers "${ATTACK_MAX_WORKERS}"
        --judge_workers "${JUDGE_MAX_WORKERS}"
    )

    "${PYTHON_BIN}" -u "${JAILBREAK_PIPELINE_SCRIPT}" "${PIPELINE_ARGS[@]}"

    cleanup_vllm
    sleep 20
}

run_judge_for_all_models() {
    local need_judge=0
    for target_model in "${TARGET_MODELS[@]}"; do
        if judge_complete "${target_model}"; then
            echo "[Judge][${target_model}] 已完成。"
        else
            need_judge=1
        fi
    done

    if [[ "${need_judge}" == "0" ]]; then
        echo "[Judge] 所有模型裁判已完成，跳过裁判模型部署。"
        return 0
    fi

    echo "[Judge] 部署裁判模型 ${JAILBREAK_JUDGE_MODEL}。"
    start_vllm "${JAILBREAK_JUDGE_MODEL}"

    for target_model in "${TARGET_MODELS[@]}"; do
        if judge_complete "${target_model}"; then
            echo "[Judge][${target_model}] 已完成，跳过。"
            continue
        fi

        "${PYTHON_BIN}" -u "${JAILBREAK_PIPELINE_SCRIPT}" \
            run \
            --data_dir "${JAILBREAK_DATA_DIR}" \
            --model_name "${target_model}" \
            --mode judge \
            --output_root "${RUN_OUTPUT_ROOT}" \
            --dispatch_dir "${RUN_DISPATCH_DIR}" \
            --judge_workers "${JUDGE_MAX_WORKERS}" \
            --judge_model "${JAILBREAK_JUDGE_MODEL}"
    done

    cleanup_vllm
    sleep 20
}

if [[ "${JAILBREAK_MODE}" == "response" || "${JAILBREAK_MODE}" == "attack" || "${JAILBREAK_MODE}" == "full" ]]; then
    for TARGET_MODEL in "${TARGET_MODELS[@]}"; do
        run_response_for_model "${TARGET_MODEL}"
    done
fi

if [[ "${JAILBREAK_MODE}" == "judge" || "${JAILBREAK_MODE}" == "full" ]]; then
    run_judge_for_all_models
    "${PYTHON_BIN}" -u "${JAILBREAK_PIPELINE_SCRIPT}" summarize --output_root "${RUN_OUTPUT_ROOT}" --models "${TARGET_MODELS[@]}"
fi

rm -rf $SINGULARITY_TMPDIR $SINGULARITY_CACHEDIR
