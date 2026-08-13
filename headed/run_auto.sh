#!/usr/bin/env bash
# 自动轮换 region 注册：失败就换下一个 region，成功就留在当前 region
#
# 用法：
#   bash run_auto.sh              # 默认累计 1 个成功就停
#   bash run_auto.sh 5            # 累计 5 个成功才停
#   TARGET=10 bash run_auto.sh    # 同上，通过环境变量
#
# 需要 config.json 里 max_tasks=1（每次只跑 1 个号，便于判定成败）

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$PROJECT_DIR/config.json"
LOG_DIR="$PROJECT_DIR/Results/auto_logs"
mkdir -p "$LOG_DIR"

# 目标成功数
TARGET="${1:-${TARGET:-1}}"

# 候选 region 列表（按优先级排）
REGIONS=(
  Alaska
  Texas
  California
  Oregon
  Virginia
  Florida
  Arizona
  Washington
  Illinois
  NewYork
)

# 每个 region 连续失败多少次就换下一个
FAIL_THRESHOLD=2

# 强制 max_tasks=1（否则无法区分单次成败）
python3 - "$CONFIG" <<'PYEOF'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
if d.get("max_tasks") != 1:
    d["max_tasks"] = 1
    json.dump(d, open(p, "w"), indent=4, ensure_ascii=False)
    print(f"[auto] 已把 max_tasks 改为 1")
PYEOF

success_count=0
total_attempts=0
region_idx=0

switch_region() {
  local new_region="${REGIONS[$region_idx]}"
  python3 - "$CONFIG" "$new_region" <<'PYEOF'
import json, re, sys
p, region = sys.argv[1], sys.argv[2]
d = json.load(open(p))
d["proxy"] = re.sub(r'region-[A-Za-z]+-st-[A-Za-z]+', f'region-US-st-{region}', d["proxy"])
json.dump(d, open(p, "w"), indent=4, ensure_ascii=False)
print(f"[auto] 切到 region: {region}")
PYEOF
}

region_fail_streak=0
switch_region

while [ "$success_count" -lt "$TARGET" ]; do
  total_attempts=$((total_attempts + 1))
  current_region="${REGIONS[$region_idx]}"
  ts=$(date +%Y%m%d_%H%M%S)
  log_file="$LOG_DIR/${ts}_${current_region}.log"

  echo ""
  echo "=========================================="
  echo "[auto] 尝试 #$total_attempts | region=$current_region | 已成功 $success_count/$TARGET"
  echo "[auto] 日志: $log_file"
  echo "=========================================="

  bash "$PROJECT_DIR/run.sh" 2>&1 | tee "$log_file"

  if grep -q "Success: TokenAuth" "$log_file"; then
    success_count=$((success_count + 1))
    region_fail_streak=0
    echo ""
    echo "[auto] ✅ 第 $success_count 个号成功（region=$current_region），继续留在该 region"
  else
    region_fail_streak=$((region_fail_streak + 1))
    echo ""
    echo "[auto] ❌ region=$current_region 连续失败 $region_fail_streak 次"
    if [ "$region_fail_streak" -ge "$FAIL_THRESHOLD" ]; then
      region_idx=$((region_idx + 1))
      region_fail_streak=0
      if [ "$region_idx" -ge "${#REGIONS[@]}" ]; then
        echo "[auto] ⚠️  所有 region 都已试过仍未达成目标，退出"
        break
      fi
      switch_region
    fi
    sleep 5
  fi
done

echo ""
echo "=========================================="
echo "[auto] 最终结果: 成功 $success_count/$TARGET, 总尝试 $total_attempts 次"
echo "[auto] 当前 region: ${REGIONS[$region_idx]}"
echo "[auto] 所有日志保存在: $LOG_DIR"
echo "=========================================="
