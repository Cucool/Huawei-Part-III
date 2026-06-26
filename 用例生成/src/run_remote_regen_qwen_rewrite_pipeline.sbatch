#!/bin/bash
#SBATCH -p gpu_ai
#SBATCH -N 1
#SBATCH -n 8
#SBATCH -G 4
#SBATCH -t 168:00:00
#SBATCH -J regen-qwen-rewrite
#SBATCH -o regen_qwen_rewrite_%J.out

set -euo pipefail

module load singularity/4.0.2
module load Anaconda/mini3-23.1.0
module load cuda/12.1.0
source activate /share/home/wuwb36g/hejx/miniconda3/envs/llm_env

WORK_DIR="/share/home/wuwb36g/projects/qwen3_deployment/code/code-vllm/low_resource_sigma_eval"
INPUT_QWEN_KEPT_DIR="${WORK_DIR}/data/filtered/qwen_rewrite"
OUTPUT_DIR="${WORK_DIR}/result_qwen_rewrite_regen_jaccard"
SCRIPT_FILE="${WORK_DIR}/run_remote_regen_qwen_rewrite_pipeline.py"
SOURCE_SEED_CACHE="${WORK_DIR}/result_true_sigma_seed_only/seed_output_cache.jsonl"

IMAGE_FILE="/share/home/wuwb36g/projects/qwen3_deployment/images/vllm_cu121.sif"
MODEL_DIR="/share/home/wuwb36g/projects/qwen3_deployment/models/Qwen2.5-7B"
MODEL_NAME="Qwen2.5-7B"
PYTHON_BIN="/share/home/wuwb36g/hejx/miniconda3/envs/llm_env/bin/python"

GPU_COUNT="${GPU_COUNT:-4}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-30000}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-128}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
REWRITE_WORKERS="${REWRITE_WORKERS:-24}"
TARGET_WORKERS="${TARGET_WORKERS:-32}"
REWRITE_TEMPERATURE="${REWRITE_TEMPERATURE:-0.7}"
TARGET_TEMPERATURE="0"
TARGET_MAX_TOKENS="${TARGET_MAX_TOKENS:-128}"
VLLM_READY_TIMEOUT_SEC="${VLLM_READY_TIMEOUT_SEC:-3600}"

JOB_TAG="${SLURM_JOB_ID:-local}"
TMP_ROOT="/tmp/${USER}/regen_qwen_rewrite/job_${JOB_TAG}"
export SINGULARITY_TMPDIR="${TMP_ROOT}/singularity_tmp"
export SINGULARITY_CACHEDIR="${TMP_ROOT}/singularity_cache"
export TORCHINDUCTOR_CACHE_DIR="${TMP_ROOT}/torchinductor"
export TRITON_CACHE_DIR="${TMP_ROOT}/triton"
export XDG_CACHE_HOME="${TMP_ROOT}/xdg"
mkdir -p \
    "${SINGULARITY_TMPDIR}" \
    "${SINGULARITY_CACHEDIR}" \
    "${TORCHINDUCTOR_CACHE_DIR}" \
    "${TRITON_CACHE_DIR}" \
    "${XDG_CACHE_HOME}" \
    "${OUTPUT_DIR}"

BASE_PORT=7700
PORT_OFFSET=$(( ${SLURM_JOB_ID:-0} % 1000 ))
VLLM_PORT=$(( BASE_PORT + PORT_OFFSET ))
OPENAI_API_BASE="http://127.0.0.1:${VLLM_PORT}/v1"
export no_proxy="localhost,127.0.0.1,::1"

echo "[INFO] host=$(hostname) job=${JOB_TAG} port=${VLLM_PORT}"
echo "[INFO] work=${WORK_DIR}"
echo "[INFO] input_qwen_kept=${INPUT_QWEN_KEPT_DIR}"
echo "[INFO] output=${OUTPUT_DIR}"

VLLM_PID=""
cleanup() {
    if [ -n "${VLLM_PID}" ] && kill -0 "${VLLM_PID}" 2>/dev/null; then
        echo "[INFO] stopping vLLM pid=${VLLM_PID}"
        kill -TERM "${VLLM_PID}" 2>/dev/null || true
        sleep 5
        kill -9 "${VLLM_PID}" 2>/dev/null || true
        wait "${VLLM_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

singularity exec --nv \
    --env GLOO_SOCKET_IFNAME=lo \
    --env NCCL_SOCKET_IFNAME=lo \
    --env CC=gcc \
    --env TORCHINDUCTOR_CACHE_DIR="${TORCHINDUCTOR_CACHE_DIR}" \
    --env TRITON_CACHE_DIR="${TRITON_CACHE_DIR}" \
    --env XDG_CACHE_HOME="${XDG_CACHE_HOME}" \
    --bind "${MODEL_DIR}:/models" \
    --bind /dev/shm:/dev/shm \
    "${IMAGE_FILE}" \
    python3 -m vllm.entrypoints.openai.api_server \
        --host 127.0.0.1 \
        --port "${VLLM_PORT}" \
        --model /models \
        --served-model-name "${MODEL_NAME}" \
        --tensor-parallel-size "${GPU_COUNT}" \
        --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
        --max-model-len "${MAX_MODEL_LEN}" \
        --max-num-seqs "${MAX_NUM_SEQS}" \
        --disable-custom-all-reduce \
        --trust-remote-code \
    > "${OUTPUT_DIR}/vllm_regen_server_${JOB_TAG}.log" 2>&1 &
VLLM_PID=$!

elapsed=0
until curl -sf --max-time 10 "${OPENAI_API_BASE}/models" > /dev/null; do
    if ! kill -0 "${VLLM_PID}" 2>/dev/null; then
        echo "[ERROR] vLLM exited before becoming ready"
        tail -200 "${OUTPUT_DIR}/vllm_regen_server_${JOB_TAG}.log" || true
        exit 1
    fi
    if [ "${elapsed}" -ge "${VLLM_READY_TIMEOUT_SEC}" ]; then
        echo "[ERROR] vLLM not ready after ${VLLM_READY_TIMEOUT_SEC}s"
        tail -200 "${OUTPUT_DIR}/vllm_regen_server_${JOB_TAG}.log" || true
        exit 1
    fi
    sleep 5
    elapsed=$(( elapsed + 5 ))
done

echo "[INFO] vLLM ready after ${elapsed}s"
curl -s "${OPENAI_API_BASE}/models" || true
echo

"${PYTHON_BIN}" -u "${SCRIPT_FILE}" \
    --input-qwen-kept-dir "${INPUT_QWEN_KEPT_DIR}" \
    --output-dir "${OUTPUT_DIR}" \
    --base-url "${OPENAI_API_BASE}" \
    --model "${MODEL_NAME}" \
    --rewrite-temperature "${REWRITE_TEMPERATURE}" \
    --target-temperature "${TARGET_TEMPERATURE}" \
    --target-max-tokens "${TARGET_MAX_TOKENS}" \
    --rewrite-workers "${REWRITE_WORKERS}" \
    --target-workers "${TARGET_WORKERS}" \
    --source-seed-cache "${SOURCE_SEED_CACHE}" \
    --no-run-seed-outputs

echo "[INFO] regenerated qwen rewrite pipeline finished"
