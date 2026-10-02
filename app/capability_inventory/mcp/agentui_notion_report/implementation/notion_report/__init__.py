"""Destination-bound Notion report creation for AgentUI."""

from .config import Destination, DestinationStore, extract_page_id
from .tool import NotionReportTool, ReportSpec

__all__ = [
    "Destination",
    "DestinationStore",
    "NotionReportTool",
    "ReportSpec",
    "extract_page_id",
]
