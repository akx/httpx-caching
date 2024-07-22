from typing import Tuple, Optional

import diskcache

from httpx_caching._models import Response
from httpx_caching._serializer import Serializer


class SyncDiskCacheCache:
    """
    httpx-caching cache backed by a `diskcache` cache.

    See https://github.com/grantjenks/python-diskcache
    """
    def __init__(
        self,
        cache: diskcache.Cache,
        serializer: Optional[Serializer] = None,
    ) -> None:
        self.cache = cache
        self.serializer = serializer or Serializer()

    def get(self, key: str) -> Tuple[Optional[Response], Optional[dict]]:
        return self.serializer.loads(self.cache.get(key, None))

    def set(
        self,
        key: str,
        response: Response,
        vary_header_data: dict,
        response_body: bytes,
    ) -> None:
        self.cache[key] = self.serializer.dumps(
            response, vary_header_data, response_body
        )

    def delete(self, key: str) -> None:
        self.cache.pop(key)

    def close(self):
        self.cache.close()
