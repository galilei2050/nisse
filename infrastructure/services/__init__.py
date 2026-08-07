from . import (
    cloud_tasks,  # noqa: F401 — side-effect: registers the Cloud Tasks queue + invoker SA
    curator_schedule,  # noqa: F401 — side-effect: registers the nightly /curate job
)
from .cloud_run_backend import backend_cloud_run_service

__all__ = ["backend_cloud_run_service"]
