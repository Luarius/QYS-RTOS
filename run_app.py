"""Entry point used by Python and the Windows executable packager."""

import os
import sys
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--qys-agent-worker":
        from qys_rtos.agent_worker import run_worker
        raise SystemExit(run_worker())
    if len(sys.argv) == 3 and sys.argv[1] == "--qys-agent-self-check":
        from qys_rtos.agent_selfcheck import run_agent_selfcheck
        raise SystemExit(run_agent_selfcheck(Path(sys.argv[2])))
    from qys_rtos.main import main
    raise SystemExit(main())
