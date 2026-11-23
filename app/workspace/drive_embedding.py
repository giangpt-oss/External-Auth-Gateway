import asyncio
import math
from collections.abc import Sequence

import httpx

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
_MAX_BATCH = 20
_FLOAT32_LIMIT = 3.4028234663852886e38


class DriveEmbeddingError(RuntimeError):
    pass


def validate_embedding(embedding: Sequence[float]) -> None:
    if len(embedding) != EMBEDDING_DIMENSIONS or not all(
        type(value) in (float, int) and abs(value) <= _FLOAT32_LIMIT and math.isfinite(value)
        for value in embedding
    ):
        raise ValueError("Invalid Drive embedding")
    if not any(abs(value) >= 1.401298464324817e-45 for value in embedding):
        raise ValueError("Drive embedding has zero magnitude")


class DriveEmbedder:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        api_key: str,
        model: str = DEFAULT_EMBEDDING_MODEL,
    ):
        self._client = http_client
        self._api_key = api_key
        self.model = model

    async def embed(
        self, texts: Sequence[str], *, timeout: float | None = None
    ) -> list[list[float]]:
        if not texts:
            return []
        if not self._api_key:
            raise DriveEmbeddingError("Drive embedding credentials are missing")

        vectors = []
        try:
            async with asyncio.timeout(timeout):
                for start in range(0, len(texts), _MAX_BATCH):
                    batch = list(texts[start:start + _MAX_BATCH])
                    vectors.extend(await self._embed_batch(batch))
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError, IndexError):
            raise DriveEmbeddingError("Drive embedding request failed") from None
        return vectors

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        response = await self._client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "input": batch,
                "model": self.model,
                "dimensions": EMBEDDING_DIMENSIONS,
                "encoding_format": "float",
            },
            follow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()["data"]
        vectors: list[list[float] | None] = [None] * len(batch)
        for item in data:
            index = item["index"]
            if type(index) is not int or not 0 <= index < len(batch) or vectors[index] is not None:
                raise ValueError("Invalid Drive embedding result index")
            vector = item["embedding"]
            if not isinstance(vector, list):
                raise ValueError("Invalid Drive embedding result")
            validate_embedding(vector)
            vectors[index] = vector
        if any(vector is None for vector in vectors):
            raise ValueError("Incomplete Drive embedding result")
        return [vector for vector in vectors if vector is not None]
