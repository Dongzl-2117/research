"""站内批量 AI 任务：摘要、观点、论点。由 HTTP 触发，进程内线程跑。"""

from researchlib.jobs import digest as digest_job
from researchlib.jobs import theses as theses_job
from researchlib.jobs import views as views_job

__all__ = ["digest_job", "views_job", "theses_job"]
