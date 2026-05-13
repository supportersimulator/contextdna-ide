"""
Indexer Agent - Content Indexing Service

The Indexer creates searchable indexes from curated content -
enabling fast retrieval and discovery.

Anatomical Label: Content Indexing Service
"""

from __future__ import annotations
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List
from collections import defaultdict

from ..base import Agent, AgentCategory, AgentState


class IndexerAgent(Agent):
    """
    Indexer Agent - Content indexing for fast retrieval.

    Responsibilities:
    - Create search indexes
    - Extract keywords and tags
    - Build inverted indexes
    - Support fast lookups
    """

    NAME = "indexer"
    CATEGORY = AgentCategory.SERVICES
    DESCRIPTION = "Content indexing for fast search and retrieval"
    ANATOMICAL_LABEL = "Content Indexing Service"
    IS_VITAL = False

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._inverted_index: Dict[str, List[str]] = defaultdict(list)  # keyword -> [doc_ids]
        self._documents: Dict[str, Dict[str, Any]] = {}  # doc_id -> document
        self._index_stats: Dict[str, int] = {
            "indexed": 0,
            "keywords": 0,
            "searches": 0
        }

    def _on_start(self):
        """Initialize indexer."""
        pass

    def _on_stop(self):
        """Shutdown indexer."""
        self._inverted_index.clear()
        self._documents.clear()

    def _check_health(self) -> Optional[Dict[str, Any]]:
        """Check indexer health."""
        return {
            "healthy": True,
            "score": 1.0,
            "message": f"Indexed {self._index_stats['indexed']} documents with {self._index_stats['keywords']} keywords",
            "metrics": self._index_stats
        }

    def process(self, input_data: Any) -> Any:
        """Process indexing operations."""
        if isinstance(input_data, dict):
            op = input_data.get("operation", "index")
            if op == "index":
                return self.index_document(input_data.get("document"))
            elif op == "search":
                return self.search(input_data.get("query"), input_data.get("limit", 10))
            elif op == "get":
                return self.get_document(input_data.get("doc_id"))
            elif op == "stats":
                return self._index_stats
        return None

    def index_document(self, document: Dict[str, Any]) -> str:
        """
        Index a document.

        Returns document ID.
        """
        if not document:
            return ""

        # Generate document ID
        content = document.get("content", json.dumps(document))
        doc_id = f"doc_{hashlib.sha256(content.encode()).hexdigest()[:12]}"

        # Store document
        self._documents[doc_id] = {
            **document,
            "_id": doc_id,
            "_indexed_at": datetime.utcnow().isoformat()
        }

        # Extract and index keywords
        keywords = self._extract_keywords(document)
        for keyword in keywords:
            if doc_id not in self._inverted_index[keyword]:
                self._inverted_index[keyword].append(doc_id)

        self._index_stats["indexed"] += 1
        self._index_stats["keywords"] = len(self._inverted_index)
        self._last_active = datetime.utcnow()

        return doc_id

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search indexed documents.

        Returns matching documents ranked by relevance.
        """
        if not query:
            return []

        self._index_stats["searches"] += 1

        # Extract query keywords
        query_keywords = self._tokenize(query.lower())

        # Find matching documents
        doc_scores: Dict[str, int] = defaultdict(int)

        for keyword in query_keywords:
            # Exact match
            for doc_id in self._inverted_index.get(keyword, []):
                doc_scores[doc_id] += 2

            # Prefix match
            for indexed_keyword in self._inverted_index:
                if indexed_keyword.startswith(keyword):
                    for doc_id in self._inverted_index[indexed_keyword]:
                        doc_scores[doc_id] += 1

        # Sort by score and return top results
        sorted_docs = sorted(doc_scores.items(), key=lambda x: x[1], reverse=True)

        results = []
        for doc_id, score in sorted_docs[:limit]:
            doc = self._documents.get(doc_id)
            if doc:
                results.append({
                    **doc,
                    "_score": score
                })

        return results

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get a document by ID."""
        return self._documents.get(doc_id)

    def remove_document(self, doc_id: str) -> bool:
        """Remove a document from the index."""
        if doc_id not in self._documents:
            return False

        # Remove from inverted index
        for keyword in list(self._inverted_index.keys()):
            if doc_id in self._inverted_index[keyword]:
                self._inverted_index[keyword].remove(doc_id)
                if not self._inverted_index[keyword]:
                    del self._inverted_index[keyword]

        # Remove document
        del self._documents[doc_id]
        self._index_stats["indexed"] -= 1
        self._index_stats["keywords"] = len(self._inverted_index)

        return True

    def _extract_keywords(self, document: Dict[str, Any]) -> List[str]:
        """Extract keywords from a document."""
        text_parts = []

        # Gather all text content
        for key, value in document.items():
            if key.startswith("_"):
                continue
            if isinstance(value, str):
                text_parts.append(value)
            elif isinstance(value, list):
                text_parts.extend(str(v) for v in value if v)

        full_text = " ".join(text_parts)
        return self._tokenize(full_text.lower())

    def _tokenize(self, text: str) -> List[str]:
        """Tokenize text into keywords."""
        # Simple tokenization
        import re
        words = re.findall(r'\b\w+\b', text)

        # Filter stopwords and short words
        stopwords = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
                     'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
                     'would', 'could', 'should', 'may', 'might', 'must', 'shall',
                     'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from',
                     'this', 'that', 'these', 'those', 'it', 'its', 'and', 'or'}

        return [w for w in words if len(w) > 2 and w not in stopwords]

    def get_keywords(self, doc_id: str = None) -> List[str]:
        """Get all keywords or keywords for a specific document."""
        if doc_id:
            doc = self._documents.get(doc_id)
            if doc:
                return self._extract_keywords(doc)
            return []
        return list(self._inverted_index.keys())
