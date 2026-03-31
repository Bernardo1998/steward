#!/usr/bin/env python3
"""Task Orchestrator — thin wrapper.

Delegates to charter_worker.orchestrator.main().
This file exists for backwards compatibility with existing cron setups
and run_daily.sh scripts that reference the root-level path.
"""
from charter_worker.orchestrator import main

if __name__ == "__main__":
    main()
