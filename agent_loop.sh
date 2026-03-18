#!/bin/bash
# agent_loop.sh — Run codex exec with auto-resume on context exhaustion
#
# Instead of one codex exec session that dies at context limit, this script
# loops: run → check if agent saved "cycle_done" in state → if not, resume.
#
# Usage (called by orchestrator instead of raw codex exec):
#   agent_loop.sh <working_dir> <instance_root> <prompt_file> <log_file> <state_file> <max_iterations> <sandbox_mode>

set -uo pipefail

WORKING_DIR="$1"
INSTANCE_ROOT="$2"
PROMPT_FILE="$3"
LOG_FILE="$4"
STATE_FILE="$5"
MAX_ITERATIONS="${6:-5}"
SANDBOX_MODE="${7:-none}"

# Build base codex flags
if [ "$SANDBOX_MODE" = "none" ]; then
    SANDBOX_FLAGS="--dangerously-bypass-approvals-and-sandbox"
else
    SANDBOX_FLAGS="--full-auto"
fi

SESSION_ID=""
ITERATION=0

while [ $ITERATION -lt $MAX_ITERATIONS ]; do
    ITERATION=$((ITERATION + 1))

    if [ $ITERATION -eq 1 ]; then
        # First run: pipe prompt from file
        echo "[agent_loop] Iteration $ITERATION: starting new session" >> "$LOG_FILE"
        SESSION_ID=$(codex exec $SANDBOX_FLAGS \
            -C "$WORKING_DIR" \
            --add-dir "$INSTANCE_ROOT" \
            --json \
            - < "$PROMPT_FILE" 2>>"$LOG_FILE" \
            | tee -a "$LOG_FILE" \
            | grep '"session_id"' | tail -1 | python3 -c "import sys,json; print(json.loads(sys.stdin.readline()).get('session_id',''))" 2>/dev/null || true)

        # Fallback: if --json didn't give us session_id, try without --json
        if [ -z "$SESSION_ID" ]; then
            codex exec $SANDBOX_FLAGS \
                -C "$WORKING_DIR" \
                --add-dir "$INSTANCE_ROOT" \
                - < "$PROMPT_FILE" >> "$LOG_FILE" 2>&1
        fi
    else
        # Resume: continue the previous session
        echo "[agent_loop] Iteration $ITERATION: resuming session" >> "$LOG_FILE"
        codex exec resume --last $SANDBOX_FLAGS \
            "Continue working on pending_steps. Read state/experiment_state.json for current progress. Keep going until all pending steps are done or you hit a hard blocker." \
            >> "$LOG_FILE" 2>&1
    fi

    # Check if agent considers itself done
    if [ -f "$STATE_FILE" ]; then
        STATUS=$(python3 -c "import json; print(json.load(open('$STATE_FILE')).get('status',''))" 2>/dev/null)
        PENDING=$(python3 -c "import json; print(len(json.load(open('$STATE_FILE')).get('pending_steps',[])))" 2>/dev/null)

        if [ "$STATUS" = "awaiting_feedback" ] || [ "$STATUS" = "done" ] || [ "$STATUS" = "validating" ]; then
            echo "[agent_loop] Stopping: status=$STATUS" >> "$LOG_FILE"
            break
        fi

        if [ "$PENDING" = "0" ]; then
            echo "[agent_loop] Stopping: no pending steps" >> "$LOG_FILE"
            break
        fi

        echo "[agent_loop] Status=$STATUS, pending=$PENDING — will resume" >> "$LOG_FILE"
    else
        echo "[agent_loop] No state file found, stopping" >> "$LOG_FILE"
        break
    fi
done

echo "[agent_loop] Completed after $ITERATION iteration(s)" >> "$LOG_FILE"
