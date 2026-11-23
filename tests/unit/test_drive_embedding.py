import json

import httpx
import pytest

from app.workspace.drive_embedding import DriveEmbedder, DriveEmbeddingError


async def test_embedding_batches_twenty_and_preserves_input_order():
    requests = []
    texts = [f"fixture {number}" for number in range(21)]

    def mock_embeddings(request):
        assert request.url == "https://api.openai.com/v1/embeddings"
        assert request.headers["authorization"] == "Bearer fixture-key"
        body = json.loads(request.content)
        assert body["model"] == "text-embedding-3-small"
        assert body["dimensions"] == 1536
        inputs = body["input"]
        requests.append(inputs)
        return httpx.Response(200, json={"data": [
            {"index": index, "embedding": [float(text.split()[-1]) + 1.0, *([0.0] * 1535)]}
            for index, text in reversed(list(enumerate(inputs)))
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_embeddings)) as client:
        result = await DriveEmbedder(client, "fixture-key").embed(texts, timeout=5)
    assert requests == [texts[:20], texts[20:]]
    assert [embedding[0] for embedding in result] == list(range(1, 22))


async def test_embedding_error_does_not_expose_request_or_provider_body():
    def service_error(request):
        return httpx.Response(429, text="private provider diagnostic")

    async with httpx.AsyncClient(transport=httpx.MockTransport(service_error)) as client:
        with pytest.raises(DriveEmbeddingError, match=r"^Drive embedding request failed$"):
            await DriveEmbedder(client, "fixture-key").embed(["private text"])
