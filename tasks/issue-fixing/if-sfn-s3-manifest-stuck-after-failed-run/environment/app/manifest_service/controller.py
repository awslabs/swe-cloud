import logging
from typing import Any

from manifest_service.async_service import (
    GenerationService,
    GenerationFailed,
    GenerationFinished,
    InvalidTokenError,
    NoSuchGeneration,
    Token,
)
from manifest_service.config import config
from manifest_service.manifests import (
    BadRequestError,
    JSON,
    ManifestKey,
    ManifestStorage,
    parse_filters,
)

log = logging.getLogger(__name__)


class ServerError(Exception):
    """
    Raised to answer a request with a 500 response. The arguments make up the
    message.
    """


class ManifestController:
    """
    Serves the `/v1/manifests` endpoints. A manifest that is in the bucket is
    served right away. For any other manifest, a generation attempt is started
    and the client gets a token to poll until the manifest is ready.
    """

    def __init__(self):
        self._async_service = GenerationService()
        self._storage = ManifestStorage()

    def request_manifest(self, *,
                         catalog: str | None,
                         format: str | None,
                         filters: str | None
                         ) -> JSON:
        """
        Handle a fresh request for a manifest: `PUT /v1/manifests`.
        """
        if catalog not in config.catalogs:
            raise BadRequestError(f'Unknown catalog {catalog!r}')
        if format not in config.manifest_formats:
            raise BadRequestError(f'Unknown manifest format {format!r}')
        filters = parse_filters(filters)
        manifest_key = ManifestKey.create(catalog=catalog, format=format, filters=filters)
        if self._storage.has_manifest(manifest_key):
            # A cache hit
            return self._found(manifest_key)
        else:
            # A cache miss: prepare the execution that will generate the
            # manifest
            token = self._start_execution(catalog=catalog,
                                          format=format,
                                          filters=filters,
                                          manifest_key=manifest_key)
            return self._redirect(token)

    def inspect_token(self, token: str) -> JSON:
        """
        Handle a client polling a token: `GET /v1/manifests/<token>`.
        """
        try:
            token = Token.decode(token)
        except InvalidTokenError:
            raise BadRequestError('Invalid token')
        try:
            token_or_result = self._async_service.inspect_generation(token)
        except NoSuchGeneration:
            raise BadRequestError('Invalid token')
        except GenerationFailed as e:
            raise ServerError('Manifest generation failed', e.status, e.error)
        if isinstance(token_or_result, Token):
            # Still running; the client keeps polling with the new token
            return self._redirect(token_or_result)
        elif isinstance(token_or_result, dict):
            # The attempt succeeded, so the manifest should be in the bucket
            result = token_or_result
            manifest_key = ManifestKey.from_json(result['input']['manifest_key'])
            if self._storage.has_manifest(manifest_key):
                return self._found(manifest_key)
            else:
                # The manifest is gone even though the attempt succeeded. Either
                # it expired, so this token is old, or it was deleted right
                # after it was written. The client hasn't been sent a 302 yet,
                # so we can still generate the manifest again in a new
                # attempt.
                input = result['input']
                token = self._start_execution(catalog=input['catalog'],
                                              format=input['format'],
                                              filters=input['filters'],
                                              manifest_key=manifest_key,
                                              previous_token=token)
                return self._redirect(token)
        else:
            assert False, token_or_result

    def _found(self, manifest_key: ManifestKey) -> JSON:
        return {
            'Status': 302,
            'Location': self._storage.manifest_location(manifest_key)
        }

    def _redirect(self, token: Token) -> JSON:
        execution_name = self._async_service.execution_name(token.generation_id, token.iteration)
        return {
            'Status': 301,
            'Location': f'/v1/manifests/{token.encode()}',
            'Retry-After': token.retry_after,
            'Execution': self._async_service.execution_arn(execution_name)
        }

    def _start_execution(self, *,
                         catalog: str,
                         format: str,
                         filters: JSON,
                         manifest_key: ManifestKey,
                         previous_token: Token | None = None,
                         ) -> Token:
        input: dict[str, Any] = {
            'catalog': catalog,
            'format': format,
            'filters': filters,
            'manifest_key': manifest_key.to_json(),
            'object_key': manifest_key.object_key
        }
        # The execution name has to fit in 80 characters, which a manifest key
        # wouldn't, so it is derived from a UUID of the key.
        #
        generation_id = manifest_key.uuid
        iteration = 0 if previous_token is None else previous_token.iteration + 1

        # A manifest lives in the bucket for `manifest_expiration` days, while
        # Step Functions keeps the name of an ended execution for 90 days.
        # Unless a manifest is deleted early, it can therefore only expire a
        # bounded number of times while the name of the attempt that produced
        # it is still taken. That bounds the number of attempts we expect for
        # one manifest. We won't come anywhere near the bound in practice:
        # reindexing the catalog, at least weekly, gives every manifest a new
        # key.
        #
        name_retention_days = 90  # set by Step Functions
        max_iteration = name_retention_days // config.manifest_expiration
        if iteration > max_iteration:
            raise ServerError('Too many attempts at generating this manifest')
        try:
            return self._async_service.start_generation(generation_id, input, iteration)
        except GenerationFinished as e:
            # The client gets the token of the attempt that ended and polls it.
            # If the manifest is still missing by then, that poll brings us
            # back here, and the next iteration is started.
            return e.token
