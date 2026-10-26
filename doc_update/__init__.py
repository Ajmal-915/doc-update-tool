"""Document Update Tool: turns approved compliance recommendations into safe, reviewable PDF changes."""

from .api import (approve_change, document_status, ingest_document, recommend_change, reject_change,
                  update_document)

__version__ = "1.0.0"

__all__ = ["approve_change", "document_status", "ingest_document", "recommend_change", "reject_change",
           "update_document"]
