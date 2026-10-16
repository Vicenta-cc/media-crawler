# -*- coding: utf-8 -*-
import httpx
import config


def make_async_client(**kwargs) -> httpx.AsyncClient:
    """创建统一配置的 httpx.AsyncClient。

    从配置文件读取 DISABLE_SSL_VERIFY（默认 False，即开启 SSL 验证）。
    仅在使用企业代理、Burp、mitmproxy 等中间人代理时才需将其设为 True。
    """
    route_snapshot = kwargs.pop('route_snapshot', None)
    if route_snapshot is not None:
        from tools.collection_route import CollectionRouteError
        if kwargs.get('mounts') or kwargs.get('verify') is False or kwargs.get('transport') is not None:
            raise CollectionRouteError('PROXY_CONFIG_INVALID')
        kwargs['proxy'] = route_snapshot.httpx_proxy()
        kwargs['trust_env'] = False
        kwargs.setdefault('verify', True)
    else:
        kwargs.setdefault("verify", not getattr(config, "DISABLE_SSL_VERIFY", False))
    return httpx.AsyncClient(**kwargs)
