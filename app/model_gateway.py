from functools import wraps
from .model_metrics import tracked_call, trace

def account(method):

    @wraps(method)
    async def wrapper(self, system_prompt, payload, model=None, **kwargs):
        return await tracked_call(self.database, method.__name__, payload, system_prompt, model or self.model, lambda: method(self, system_prompt, payload, model=model, **kwargs))
    return wrapper

class ModelGateway:
    provider = 'gateway'

    def __init__(self, local, remote=None, automatic_model=''):
        self.database = None
        self.local = local
        self.remote = None
        self.model = local.model
        self.configure(remote, automatic_model)

    @property
    def remote_models(self):
        return list(self.remote.installed) if self.remote else []

    def configure(self, remote=None, automatic_model=''):
        self.remote = remote if remote is not None and remote.configured else None
        self.model = automatic_model or self.local.model

    def client_for(self, model):
        if self.remote is not None and self.remote.serves(model):
            return self.remote
        return self.local

    async def models(self):
        names = set(await self.local.models())
        if self.remote is not None:
            names.update(await self.remote.models())
        return sorted(names)

    async def healthy(self):
        return await self.local.healthy()

    async def _resolve(self, model):
        chosen = model or self.model
        if self.remote is not None and self.remote.serves(chosen):
            return (self.remote, chosen)
        if chosen != self.local.model:
            lister = getattr(self.local, 'models', None)
            if lister is not None and chosen not in await lister():
                return (self.local, self.local.model)
        return (self.local, chosen)

    async def provider_for(self, model):
        client, _ = await self._resolve(model)
        return str(getattr(client, 'provider', ''))

    @account
    async def analyze(self, system_prompt, payload, model=None, context_tokens=None):
        client, chosen = await self._resolve(model)
        if trace.get() is not None:
            trace.get()['model'] = str(chosen)[:120]
        if context_tokens is None:
            return await client.analyze(system_prompt, payload, model=chosen)
        return await client.analyze(system_prompt, payload, model=chosen, context_tokens=context_tokens)

    @account
    async def analyze_questions(self, system_prompt, payload, model=None):
        client, chosen = await self._resolve(model)
        if trace.get() is not None:
            trace.get()['model'] = str(chosen)[:120]
        return await client.analyze_questions(system_prompt, payload, model=chosen)

    @account
    async def analyze_segment(self, system_prompt, payload, model=None, context_tokens=None, wall_seconds=None):
        client, chosen = await self._resolve(model)
        segment_client = getattr(client, 'analyze_segment', None)
        if trace.get() is not None:
            trace.get()['model'] = str(chosen)[:120]
        if segment_client is None:
            return await client.analyze(system_prompt, payload, model=chosen)
        return await segment_client(system_prompt, payload, model=chosen, context_tokens=context_tokens, wall_seconds=wall_seconds)
