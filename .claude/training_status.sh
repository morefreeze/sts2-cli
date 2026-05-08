#!/bin/zsh
# Project status line: pass-through ccstatusline + training progress.
# Reads Claude-Code session JSON on stdin, forwards it to ccstatusline,
# then appends a one-line training-progress summary on the next line.
set -u

# Capture stdin once (fork once into both consumers)
input=$(cat)

# Run ccstatusline using the captured stdin
cc_out=$(echo "$input" | npx -y ccstatusline@latest 2>/dev/null)

# Detect active training run by reading latest training_run*.log and matching PID
cd /Users/bytedance/mygit/sts2-cli
train_pid=$(pgrep -f 'agent.train' | head -1)

if [[ -n "$train_pid" ]]; then
    # Find newest training log
    log=$(ls -t training_run*.log 2>/dev/null | head -1)
    if [[ -n "$log" ]]; then
        # Pull the most recent progress line. They're \r-overwritten one-liners,
        # so we grab the tail and split on \r.
        last=$(tail -c 1500 "$log" 2>/dev/null | tr '\r' '\n' | grep -E "^\s*\[\s*[0-9]+\.[0-9]+%\].*\|" | tail -1)
        if [[ -n "$last" ]]; then
            # Parse: "[ XX.X%] step/total | sps sps | ETA ... | iter ..% | floor=X.X cwr=X% cr=X%"
            pct=$(echo "$last" | grep -oE '\[\s*[0-9]+\.[0-9]+%\]' | tr -d '[ %]' | head -1)
            steps=$(echo "$last" | grep -oE '[0-9]+/[0-9]+' | head -1)
            sps=$(echo "$last" | grep -oE '[0-9]+ sps' | head -1)
            eta=$(echo "$last" | grep -oE 'ETA [0-9hms]+' | head -1 | sed 's/ETA //')
            metrics=$(echo "$last" | grep -oE 'floor=[0-9.]+ cwr=[0-9]+%( cr=[0-9]+%)?' | head -1)
            run_n=$(echo "$log" | grep -oE 'run[0-9]+' | grep -oE '[0-9]+')
            echo "$cc_out"
            echo "🎓 Run${run_n} ${pct}% (${steps}) ${sps} ETA ${eta} | ${metrics}"
            exit 0
        fi
    fi
    echo "$cc_out"
    echo "🎓 Training PID=$train_pid (warming up)"
    exit 0
fi

# No training — show last known result if available
if [[ -f /tmp/autoloop.log ]]; then
    last_eval=$(grep -E "Eval result|REGRESSION|ACCEPT|MARGINAL" /tmp/autoloop.log 2>/dev/null | tail -1 | sed 's/\[autoloop[^]]*\] //')
    if [[ -n "$last_eval" ]]; then
        echo "$cc_out"
        echo "💤 No training. Last: $last_eval"
        exit 0
    fi
fi

echo "$cc_out"
