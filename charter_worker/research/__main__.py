"""Allow running as: python -m charter_worker.research.cli"""
from .cli import main
import sys
sys.exit(main())
