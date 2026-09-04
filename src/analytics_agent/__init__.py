"""analytics-agent — Student C's plug point in the local lakehouse agent.

Phase 1: ``lumid_gateway``, a lumid-data-app-compatible data plane over
PostgreSQL + MinIO. Phase 2 adds a complete internal ``JobSpec`` planning
contract while retaining the DAG-returning ``nl2workflow`` compatibility API.
"""

__version__ = "0.1.0"

from .api import PlanningContext, plan

__all__ = ["PlanningContext", "plan"]
