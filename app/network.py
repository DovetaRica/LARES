"""Outbound policy for the optional application; no implicit network access."""
import httpx
from home_ai.config import validate_endpoint


def check_endpoint(url, settings=None):
    if settings is None:
        from .config import settings
    if not settings.lares_enable_network:
        raise PermissionError('Application networking is disabled')
    return validate_endpoint(str(url), settings.lares_allow_remote, 'application')


class AsyncClient(httpx.AsyncClient):
    def __init__(self, *args, **kwargs):
        self._lares_policy = kwargs.pop('network_policy', None)
        kwargs.update(trust_env=False, follow_redirects=False)
        super().__init__(*args, **kwargs)

    async def send(self, request, **kwargs):
        check_endpoint(request.url, self._lares_policy)
        kwargs['follow_redirects'] = False
        return await super().send(request, **kwargs)
