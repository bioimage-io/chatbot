import sys
import io
import os
from functools import partial
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional, Union
from bioimageio_chatbot.knowledge_base import load_knowledge_base
from bioimageio_chatbot.utils import get_manifest
from bioimageio_chatbot.utils import ChatbotExtension


class DocWithScore(BaseModel):
    """A document with an associated relevance score."""

    doc: str = Field(description="The document retrieved.")
    score: float = Field(description="The relevance score of the retrieved document.")
    metadata: Dict[str, Any] = Field(description="The document's metadata.")
    base_url: Optional[str] = Field(
        None,
        description="The documentation's base URL, which will be used to resolve the relative URLs in the retrieved document chunks when producing markdown links.",
    )


class DocumentSearchInput(BaseModel):
    """Results of a document retrieval process from a documentation base."""

    user_question: str = Field(description="The user's original question.")
    relevant_context: List[DocWithScore] = Field(
        description="Chunks of context retrieved from the documentation that are relevant to the user's original question."
    )
    user_info: Optional[str] = Field(
        "", description="The user's info for personalizing the response."
    )
    format: Optional[str] = Field(None, description="The format of the document.")
    preliminary_response: Optional[str] = Field(
        None,
        description="The preliminary response to the user's question. This will be combined with the retrieved documents to produce the Document Response.",
    )


knowledge_base_path = os.environ.get(
    "BIOIMAGEIO_KNOWLEDGE_BASE_PATH", "./bioimageio-knowledge-base"
)
docs_store_dict = load_knowledge_base(knowledge_base_path)


async def get_schema(channel_id):
    class DocumentRetrievalInput(BaseModel):
        """Input for searching knowledge bases and finding documents relevant to the user's request."""

        request: str = Field(description="The user's detailed request")
        preliminary_response: str = Field(
            description="The preliminary response to the user's question. This will be combined with the retrieved documents to produce the final response."
        )
        query: str = Field(
            description="The query used to retrieve documents related to the user's request. Take preliminary_response as reference to generate query if needed."
        )
        user_info: Optional[str] = Field(
            "",
            description="Brief user info summary including name, background, etc., for personalizing responses to the user.",
        )

    DocumentRetrievalInput.__name__ = channel_id.replace(".", "_").replace("-", "_")
    return DocumentRetrievalInput.schema()


async def run_extension(channel_id, req):
    collections = get_manifest()["collections"]
    docs_store = docs_store_dict[channel_id]
    collection_info_dict = {collection["id"]: collection for collection in collections}

    collection_info = collection_info_dict[channel_id]
    base_url = collection_info.get("base_url")
    print(f"Retrieving documents from database {channel_id} with query: {req.query}")
    results_with_scores = await docs_store.asimilarity_search_with_relevance_scores(
        req.query, k=3
    )
    docs_with_score = [
        DocWithScore(
            doc=doc.page_content, score=score, metadata=doc.metadata, base_url=base_url
        )
        for doc, score in results_with_scores
    ]
    print(
        f"Retrieved documents:\n{docs_with_score[0].doc[:20] + '...'} (score: {docs_with_score[0].score})\n{docs_with_score[1].doc[:20] + '...'} (score: {docs_with_score[1].score})\n{docs_with_score[2].doc[:20] + '...'} (score: {docs_with_score[2].score})"
    )
    return docs_with_score


def get_extensions():
    collections = get_manifest()["collections"]
    return [
        ChatbotExtension(
            name=collection["id"],
            description=collection["description"],
            get_schema=partial(get_schema, collection["id"]),
            execute=partial(run_extension, collection["id"]),
        )
        for collection in collections
    ]
