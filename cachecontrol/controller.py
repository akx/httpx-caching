# SPDX-FileCopyrightText: 2015 Eric Larson
#
# SPDX-License-Identifier: Apache-2.0

"""
The httplib2 algorithms ported for use with requests.
"""
import logging
import calendar
import time
from email.utils import parsedate_tz

from .cache import DictCache
from .serialize import Serializer


logger = logging.getLogger(__name__)

PERMANENT_REDIRECT_STATUSES = (301, 308)


class CacheController(object):
    """An interface to see if request should cached or not.
    """

    def __init__(
        self, cache=None, cache_etags=True, serializer=None, status_codes=None
    ):
        self.cache = DictCache() if cache is None else cache
        self.cache_etags = cache_etags
        self.serializer = serializer or Serializer()
        self.cacheable_status_codes = status_codes or (200, 203, 300, 301, 308)

    @classmethod
    def cache_url(cls, uri):
        if not uri.scheme or not uri.authority:
            raise Exception("Only absolute URIs are allowed. uri = %s" % uri)
        return str(uri)

    def parse_cache_control(self, headers):
        known_directives = {
            # https://tools.ietf.org/html/rfc7234#section-5.2
            "max-age": (int, True),
            "max-stale": (int, False),
            "min-fresh": (int, True),
            "no-cache": (None, False),
            "no-store": (None, False),
            "no-transform": (None, False),
            "only-if-cached": (None, False),
            "must-revalidate": (None, False),
            "public": (None, False),
            "private": (None, False),
            "proxy-revalidate": (None, False),
            "s-maxage": (int, True),
        }

        cc_headers = headers.get("cache-control", "")

        retval = {}

        for cc_directive in cc_headers.split(","):
            if not cc_directive.strip():
                continue

            parts = cc_directive.split("=", 1)
            directive = parts[0].strip()

            try:
                typ, required = known_directives[directive]
            except KeyError:
                logger.debug("Ignoring unknown cache-control directive: %s", directive)
                continue

            if not typ or not required:
                retval[directive] = None
            if typ:
                try:
                    retval[directive] = typ(parts[1].strip())
                except IndexError:
                    if required:
                        logger.debug(
                            "Missing value for cache-control " "directive: %s",
                            directive,
                        )
                except ValueError:
                    logger.debug(
                        "Invalid value for cache-control directive " "%s, must be %s",
                        directive,
                        typ.__name__,
                    )

        return retval

    def cached_request(self, request_url, request_headers):
        """
        Return a cached response if it exists in the cache, otherwise
        return False.
        """
        cache_url = self.cache_url(request_url)
        logger.debug('Looking up "%s" in the cache', cache_url)
        cc = self.parse_cache_control(request_headers)

        # Bail out if the request insists on fresh data
        if "no-cache" in cc:
            logger.debug('Request header has "no-cache", cache bypassed')
            return False

        if "max-age" in cc and cc["max-age"] == 0:
            logger.debug('Request header has "max_age" as 0, cache bypassed')
            return False

        # Request allows serving from the cache, let's see if we find something
        cache_data = self.cache.get(cache_url)
        if cache_data is None:
            logger.debug("No cache entry available")
            return False

        # Check whether it can be deserialized
        resp = self.serializer.loads(request_headers, cache_data)
        if not resp:
            logger.warning("Cache entry deserialization failed, entry ignored")
            return False

        # If we have a cached permanent redirect, return it immediately. We
        # don't need to test our response for other headers b/c it is
        # intrinsically "cacheable" as it is Permanent.
        #
        # See:
        #   https://tools.ietf.org/html/rfc7231#section-6.4.2
        #
        # Client can try to refresh the value by repeating the request
        # with cache busting headers as usual (ie no-cache).
        if int(resp.status) in PERMANENT_REDIRECT_STATUSES:
            msg = (
                'Returning cached permanent redirect response '
                "(ignoring date and etag information)"
            )
            logger.debug(msg)
            return resp

        if not request_headers or "date" not in request_headers:
            if "etag" not in request_headers:
                # Without date or etag, the cached response can never be used
                # and should be deleted.
                logger.debug("Purging cached response: no date or etag")
                self.cache.delete(cache_url)
            logger.debug("Ignoring cached response: no date")
            return False

        now = time.time()
        date = calendar.timegm(parsedate_tz(request_headers["date"]))
        current_age = max(0, now - date)
        logger.debug("Current age based on date: %i", current_age)

        # TODO: There is an assumption that the result will be a
        #       urllib3 response object. This may not be best since we
        #       could probably avoid instantiating or constructing the
        #       response until we know we need it.
        resp_cc = self.parse_cache_control(request_headers)

        # determine freshness
        freshness_lifetime = 0

        # Check the max-age pragma in the cache control header
        if "max-age" in resp_cc:
            freshness_lifetime = resp_cc["max-age"]
            logger.debug("Freshness lifetime from max-age: %i", freshness_lifetime)

        # If there isn't a max-age, check for an expires header
        elif "expires" in request_headers:
            expires = parsedate_tz(request_headers["expires"])
            if expires is not None:
                expire_time = calendar.timegm(expires) - date
                freshness_lifetime = max(0, expire_time)
                logger.debug("Freshness lifetime from expires: %i", freshness_lifetime)

        # Determine if we are setting freshness limit in the
        # request. Note, this overrides what was in the response.
        if "max-age" in cc:
            freshness_lifetime = cc["max-age"]
            logger.debug(
                "Freshness lifetime from request max-age: %i", freshness_lifetime
            )

        if "min-fresh" in cc:
            min_fresh = cc["min-fresh"]
            # adjust our current age by our min fresh
            current_age += min_fresh
            logger.debug("Adjusted current age from min-fresh: %i", current_age)

        # Return entry if it is fresh enough
        if freshness_lifetime > current_age:
            logger.debug('The response is "fresh", returning cached response')
            logger.debug("%i > %i", freshness_lifetime, current_age)
            return resp

        # we're not fresh. If we don't have an Etag, clear it out
        if "etag" not in request_headers:
            logger.debug('The cached response is "stale" with no etag, purging')
            self.cache.delete(cache_url)

        # return the original handler
        return False

    def conditional_headers(self, request_url):
        cache_url = self.cache_url(request_url)
        resp = self.serializer.loads(request_headers, self.cache.get(cache_url))
        new_headers = {}

        if resp:
            headers = resp.headers.copy()

            if "etag" in headers:
                new_headers["If-None-Match"] = headers["ETag"]

            if "last-modified" in headers:
                new_headers["If-Modified-Since"] = headers["Last-Modified"]

        return new_headers

    def cache_response(
            self,
            request_url,
            request_headers,
            response_headers,
            response_status_code,
            response_reason_phrase,
            response_http_version,
            response_body,
            ):
        """
        Algorithm for caching requests.
        """
        # From httplib2: Don't cache 206's since we aren't going to
        #                handle byte range requests
        if response_status_code not in self.cacheable_status_codes:
            logger.debug(
                "Status code %s not in %s", response.status, self.cacheable_status_codes
            )
            return

        # If we've been given a body, our response has a Content-Length, that
        # Content-Length is valid then we can check to see if the body we've
        # been given matches the expected size, and if it doesn't we'll just
        # skip trying to cache it.
        if (
            response_body is not None
            and "content-length" in response_headers
            and response_headers["content-length"].isdigit()
            and int(response_headers["content-length"]) != len(response_body)
        ):
            return

        cc_req = self.parse_cache_control(request_headers)
        cc = self.parse_cache_control(response_headers)

        cache_url = self.cache_url(request_url)
        logger.debug('Updating cache with response from "%s"', cache_url)

        # Delete it from the cache if we happen to have it stored there
        no_store = False
        if "no-store" in cc:
            no_store = True
            logger.debug('Response header has "no-store"')
        if "no-store" in cc_req:
            no_store = True
            logger.debug('Request header has "no-store"')
        if no_store and self.cache.get(cache_url):
            logger.debug('Purging existing cache entry to honor "no-store"')
            self.cache.delete(cache_url)
        if no_store:
            return

        # https://tools.ietf.org/html/rfc7234#section-4.1:
        # A Vary header field-value of "*" always fails to match.
        # Storing such a response leads to a deserialization warning
        # during cache lookup and is not allowed to ever be served,
        # so storing it can be avoided.
        if "*" in response_headers.get("vary", ""):
            logger.debug('Response header has "Vary: *"')
            return

        # If we've been given an etag, then keep the response
        if self.cache_etags and "etag" in response_headers:
            logger.debug("Caching due to etag")

        # Add to the cache any permanent redirects. We do this before looking
        # that the Date headers.
        elif int(response_status_code) in PERMANENT_REDIRECT_STATUSES:
            logger.debug("Caching permanent redirect")
            response_body = b''

        # Add to the cache if the response headers demand it. If there
        # is no date header then we can't do anything about expiring
        # the cache.
        elif "date" in response_headers:
            # cache when there is a max-age > 0
            if "max-age" in cc and cc["max-age"] > 0:
                logger.debug("Caching b/c date exists and max-age > 0")

            # If the request can expire, it means we should cache it
            # in the meantime.
            elif "expires" in response_headers:
                if response_headers["expires"]:
                    logger.debug("Caching b/c of expires header")
            else:
                return
        else:
            return

        self.cache.set(
            cache_url, self.serializer.dumps(
                request_headers,
                response_headers,
                response_status_code,
                response_http_version,
                response_reason_phrase,
                response_body
            )
        )

    def update_cached_response(self, request_url, request_headers, response_headers):
        """On a 304 we will get a new set of headers that we want to
        update our cached value with, assuming we have one.

        This should only ever be called when we've sent an ETag and
        gotten a 304 as the response.
        """
        cache_url = self.cache_url(request_url)

        cached_response = self.serializer.loads(request_headers, self.cache.get(cache_url))

        if not cached_response:
            # we didn't have a cached response
            return
        else:
            (
                cached_http_version,
                cached_status_code,
                cached_reason_phrase,
                cached_headers,
                cached_body
            ) = cached_response

        # Lets update our headers with the headers from the new request:
        # http://tools.ietf.org/html/draft-ietf-httpbis-p4-conditional-26#section-4.1
        #
        # The server isn't supposed to send headers that would make
        # the cached body invalid. But... just in case, we'll be sure
        # to strip out ones we know that might be problematic due to
        # typical assumptions.
        excluded_headers = ["content-length"]

        cached_headers.update(
            dict(
                (k, v)
                for k, v in response_headers.items()
                if k.lower() not in excluded_headers
            )
        )

        # we want a 200 b/c we have content via the cache
        cached_status_code = 200

        # update our cache
        body = cached_body.read(decode_content=False)
        self.cache.set(
            cache_url,
            self.serializer.dumps(
                request_headers,
                cached_headers,
                cached_status_code,
                cached_http_version,
                cached_reason_phrase,
                body
            )
        )

        return (
            cached_http_version,
            cached_status_code,
            cached_reason_phrase,
            cached_headers,
            cached_body
        )
