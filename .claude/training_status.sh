#!/bin/zsh
# Project status line: pass-through ccstatusline + multi-task RL progress.
# Reads Claude-Code session JSON on stdin, forwards it to ccstatusline,
# then appends 1-3 extra lines summarizing concurrent RL tasks.
#
# Lines added (in order, only shown when relevant):
#   🎓 train  - if any `agent.train` process is running
#   🔬 eval   - if any `agent.eval_rl` process is running
#   🎯 retry  - if any `boss_retry` process is running
#   📊 latest - most-recent finished eval/retry summary from /tmp/sts2-cli/
#   💤 idle   - shown only when no train/eval/retry is running
set -u

input=$(cat)
cc_out=$(echo "$input" | npx -y ccstatusline@latest 2>/dev/null)
echo "$cc_out"

cd /Users/bytedance/mygit/sts2-cli

# ───── helper: parse a training_run*.log progress line ────────────────────
fmt_train() {
    local log="$1"
    local last
    last=$(tail -c 1500 "$log" 2>/dev/null | tr '\r' '\n' \
           | grep -E "^\s*\[\s*[0-9]+\.[0-9]+%\].*\|" | tail -1)
    [[ -z "$last" ]] && { echo "(warming up)"; return; }
    local pct steps sps eta metrics tag eta_secs h m s done_at
    pct=$(echo "$last" | grep -oE '\[\s*[0-9]+\.[0-9]+%\]' | tr -d '[ %]' | head -1)
    steps=$(echo "$last" | grep -oE '[0-9]+/[0-9]+' | head -1)
    sps=$(echo "$last" | grep -oE '[0-9]+ sps' | head -1)
    eta=$(echo "$last" | grep -oE 'ETA [0-9hms]+' | head -1 | sed 's/ETA //')
    metrics=$(echo "$last" | grep -oE 'floor=[0-9.]+ cwr=[0-9]+%( cr=[0-9]+%)?( to=[0-9]+%)?' | head -1)
    tag=$(basename "$log" .log | sed 's/training_//')
    h=$(echo "$eta" | grep -oE '[0-9]+h' | tr -d 'h')
    m=$(echo "$eta" | grep -oE '[0-9]+m' | tr -d 'm')
    s=$(echo "$eta" | grep -oE '[0-9]+s' | tr -d 's')
    eta_secs=$(( ${h:-0} * 3600 + ${m:-0} * 60 + ${s:-0} ))
    if (( eta_secs > 0 )); then
        done_at=$(date -v+${eta_secs}S '+%H:%M')
        echo "${tag} ${pct}% (${steps}) ${sps} ETA ${eta}→${done_at} | ${metrics}"
    else
        echo "${tag} ${pct}% (${steps}) ${sps} | ${metrics}"
    fi
}

# ───── train ──────────────────────────────────────────────────────────────
train_pids=$(pgrep -f 'agent\.train' 2>/dev/null)
if [[ -n "$train_pids" ]]; then
    log=$(ls -t training_run*.log training_boss*.log 2>/dev/null | head -1)
    if [[ -n "$log" ]]; then
        echo "🎓 $(fmt_train "$log")"
    else
        echo "🎓 train PID=$(echo $train_pids | tr '\n' ' ') (no log)"
    fi
fi

# ───── eval ───────────────────────────────────────────────────────────────
eval_pids=$(pgrep -f 'agent\.eval_rl' 2>/dev/null)
if [[ -n "$eval_pids" ]]; then
    eval_log=$(ls -t /tmp/sts2-cli/eval_*.log 2>/dev/null | head -1)
    if [[ -n "$eval_log" ]]; then
        n_done=$(grep -cE "^  game " "$eval_log" 2>/dev/null)
        ckpt=$(basename "$eval_log" .log | sed 's/^eval_//')
        echo "🔬 eval ${ckpt}: ${n_done} games done"
    else
        echo "🔬 eval PID=$(echo $eval_pids | tr '\n' ' ') (starting)"
    fi
fi

# ───── boss_retry ─────────────────────────────────────────────────────────
retry_pids=$(pgrep -f 'boss_retry' 2>/dev/null)
if [[ -n "$retry_pids" ]]; then
    retry_log=$(ls -t /tmp/sts2-cli/boss_retry_*.log /tmp/sts2-cli/hp_sweep_*.log 2>/dev/null | head -1)
    if [[ -n "$retry_log" ]]; then
        # boss_retry prints "  mode: win X/Y …" lines once each mode completes
        n_done=$(grep -cE "^  (deterministic|stochastic|hp=)" "$retry_log" 2>/dev/null)
        ckpt=$(basename "$retry_log" .log | sed 's/^boss_retry_//;s/^hp_sweep_//')
        echo "🎯 retry ${ckpt}: ${n_done} segments done"
    else
        echo "🎯 retry PID=$(echo $retry_pids | tr '\n' ' ')"
    fi
fi

# ───── most recent finished eval/retry summary ────────────────────────────
# Only show if NO train/eval/retry currently running OR as a tail tip.
latest_log=$(ls -t /tmp/sts2-cli/eval_*.log /tmp/sts2-cli/boss_retry_*.log /tmp/sts2-cli/hp_sweep_*.log 2>/dev/null \
             | head -1)
if [[ -n "$latest_log" && -z "$eval_pids" && -z "$retry_pids" ]]; then
    # Only when the corresponding task is no longer running — show summary
    avg=$(grep -E "^avg_floor" "$latest_log" 2>/dev/null | tail -1 | awk '{print $3}')
    win=$(grep -E "^win_rate" "$latest_log" 2>/dev/null | tail -1 | awk '{print $3}')
    name=$(basename "$latest_log" .log)
    age=$(( $(date +%s) - $(stat -f %m "$latest_log" 2>/dev/null) ))
    age_str=$(( age / 60 ))m
    if [[ -n "$avg" ]]; then
        echo "📊 ${name}: avg_floor=${avg} win=${win} (${age_str} ago)"
    fi
fi

# ───── idle marker if nothing else printed ────────────────────────────────
if [[ -z "$train_pids" && -z "$eval_pids" && -z "$retry_pids" ]]; then
    echo "💤 No RL tasks running"
fi
