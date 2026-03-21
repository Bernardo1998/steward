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

        # Hard stops: done, or initial plan phase awaiting feedback
        if [ "$STATUS" = "done" ]; then
            echo "[agent_loop] Stopping: status=done" >> "$LOG_FILE"
            break
        fi

        if [ "$STATUS" = "needs_plan" ]; then
            echo "[agent_loop] Stopping: status=needs_plan (first run not started)" >> "$LOG_FILE"
            break
        fi

        # awaiting_feedback only stops during initial plan approval phase
        # (before any implementing has happened). Once the agent has been
        # implementing, it should never set awaiting_feedback again.
        if [ "$STATUS" = "awaiting_feedback" ]; then
            COMPLETED=$(python3 -c "import json; print(len(json.load(open('$STATE_FILE')).get('completed_steps',[])))" 2>/dev/null)
            if [ "$COMPLETED" = "0" ]; then
                echo "[agent_loop] Stopping: awaiting_feedback (initial plan phase)" >> "$LOG_FILE"
                break
            else
                echo "[agent_loop] WARNING: awaiting_feedback set post-plan-phase (completed=$COMPLETED). Overriding to continue." >> "$LOG_FILE"
                # Don't break — resume the agent so it keeps working
            fi
        fi

        # Don't stop on empty pending_steps — the agent should generate new steps
        # Only stop if pending=0 AND status=done (handled above)
        if [ "$PENDING" = "0" ] && [ "$STATUS" = "implementing" ]; then
            echo "[agent_loop] pending_steps empty but status=implementing — resuming so agent can generate new steps" >> "$LOG_FILE"
        fi

        echo "[agent_loop] Status=$STATUS, pending=$PENDING — will resume" >> "$LOG_FILE"
    else
        echo "[agent_loop] No state file found, stopping" >> "$LOG_FILE"
        break
    fi
done

echo "[agent_loop] Completed after $ITERATION iteration(s)" >> "$LOG_FILE"
