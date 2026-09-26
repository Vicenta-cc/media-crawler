from unittest.mock import AsyncMock

import httpx
import pytest

from media_platform.douyin.client import DouYinClient
from media_platform.douyin.exception import DataFetchError


@pytest.mark.asyncio
@pytest.mark.parametrize('primary', [True, False])
async def test_detail_uses_current_same_account_identity_without_mutating_shared_headers(primary):
    client = DouYinClient.__new__(DouYinClient)
    client.headers = {'Origin': 'https://www.douyin.com/', 'Cookie': 'test', 'uifid': 'stale'}
    client.cookie_dict = {'UIFID_TEMP': 'temporary', 's_v_web_id': 'same-account-fp'}
    if primary:
        client.cookie_dict['UIFID'] = 'primary'
    client.get = AsyncMock(return_value={'aweme_detail': {'aweme_id': '123'}})
    assert await client.get_video_by_id('123', operation='media_refresh') == {'aweme_id': '123'}
    _, params, headers = client.get.await_args.args
    assert params['uifid'] == headers['uifid'] == ('primary' if primary else 'temporary')
    assert params['verifyFp'] == params['fp'] == 'same-account-fp'
    assert headers['x-tt-argus'] == '1' and 'Origin' not in headers
    assert client.get.await_args.kwargs == {'operation': 'media_refresh'}
    assert client.headers == {'Origin': 'https://www.douyin.com/', 'Cookie': 'test', 'uifid': 'stale'}


@pytest.mark.asyncio
async def test_gateway_rejection_still_fails_after_identity_compatibility():
    client = DouYinClient.__new__(DouYinClient)
    client._host = 'https://www.douyin.com'
    client.headers = {'User-Agent': 'test'}
    client.cookie_dict = {'UIFID': 'identity', 's_v_web_id': 'fp'}
    client._DouYinClient__process_req_params = AsyncMock()
    client._send = AsyncMock(return_value=httpx.Response(403, text='Blocked by ArgusSecurityPlugin Signature Not Found'))
    with pytest.raises(DataFetchError, match='ACCOUNT_VERIFY'):
        await client.get_video_by_id('123')
