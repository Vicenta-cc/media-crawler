# -*- coding: utf-8 -*-
import pytest
import config
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from cmd_arg import parse_cmd
from api.schemas import CrawlerStartRequest, PlatformEnum, LoginTypeEnum, CrawlerTypeEnum
from api.services.crawler_manager import CrawlerManager
from api.main import app

@pytest.mark.asyncio
async def test_cmd_arg_crawler_max_notes_count():
    # Store original values
    orig_notes = config.CRAWLER_MAX_NOTES_COUNT
    orig_comments = config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES
    orig_items_per_minute = config.CRAWLER_MAX_ITEMS_PER_MINUTE
    orig_concurrency = config.MAX_CONCURRENCY_NUM

    try:
        await parse_cmd([
            "--platform", "xhs",
            "--crawler_max_notes_count", "42",
            "--max_comments_count_singlenotes", "24",
            "--crawler_max_items_per_minute", "3",
            "--max_concurrency_num", "2",
        ])
        assert config.CRAWLER_MAX_NOTES_COUNT == 42
        assert config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES == 24
        assert config.CRAWLER_MAX_ITEMS_PER_MINUTE == 3
        assert config.MAX_CONCURRENCY_NUM == 2
    finally:
        config.CRAWLER_MAX_NOTES_COUNT = orig_notes
        config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = orig_comments
        config.CRAWLER_MAX_ITEMS_PER_MINUTE = orig_items_per_minute
        config.MAX_CONCURRENCY_NUM = orig_concurrency

def test_crawler_manager_build_command():
    cm = CrawlerManager()

    # 1. No max limits passed in API request
    req1 = CrawlerStartRequest(
        platform=PlatformEnum.XHS,
        login_type=LoginTypeEnum.QRCODE,
        crawler_type=CrawlerTypeEnum.SEARCH,
        keywords="test",
        max_notes_count=None,
        max_comments_count=None
    )
    cmd1 = cm._build_command(req1)
    # Check that the custom arguments are NOT present
    assert "--crawler_max_notes_count" not in cmd1
    assert "--max_comments_count_singlenotes" not in cmd1
    rate_index = cmd1.index("--crawler_max_items_per_minute")
    assert cmd1[rate_index + 1] == "5"
    concurrency_index = cmd1.index("--max_concurrency_num")
    assert cmd1[concurrency_index + 1] == "1"

    # 2. Both limits passed in API request
    req2 = CrawlerStartRequest(
        platform=PlatformEnum.XHS,
        login_type=LoginTypeEnum.QRCODE,
        crawler_type=CrawlerTypeEnum.SEARCH,
        keywords="test",
        max_notes_count=50,
        max_comments_count=5,
        max_items_per_minute=2,
        max_concurrency_num=3,
    )
    cmd2 = cm._build_command(req2)
    # Check that they are correctly added
    assert "--crawler_max_notes_count" in cmd2
    idx_notes = cmd2.index("--crawler_max_notes_count")
    assert cmd2[idx_notes + 1] == "50"

    assert "--max_comments_count_singlenotes" in cmd2
    idx_comments = cmd2.index("--max_comments_count_singlenotes")
    assert cmd2[idx_comments + 1] == "5"

    idx_rate = cmd2.index("--crawler_max_items_per_minute")
    assert cmd2[idx_rate + 1] == "2"
    idx_concurrency = cmd2.index("--max_concurrency_num")
    assert cmd2[idx_concurrency + 1] == "3"

def test_api_start_crawler_with_limits():
    client = TestClient(app)

    with patch("api.routers.crawler.crawler_manager.start", new_callable=AsyncMock) as mock_start:
        mock_start.return_value = True

        # Test case 1: with limits
        response = client.post("/api/crawler/start", json={
            "platform": "xhs",
            "login_type": "qrcode",
            "crawler_type": "search",
            "keywords": "test",
            "max_notes_count": 50,
            "max_comments_count": 5,
            "max_items_per_minute": 3,
            "max_concurrency_num": 2,
        })

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "message": "Crawler started successfully"}

        mock_start.assert_called_once()
        called_request = mock_start.call_args[0][0]
        assert called_request.platform == PlatformEnum.XHS
        assert called_request.max_notes_count == 50
        assert called_request.max_comments_count == 5
        assert called_request.max_items_per_minute == 3
        assert called_request.max_concurrency_num == 2

def test_api_start_crawler_without_limits():
    client = TestClient(app)

    with patch("api.routers.crawler.crawler_manager.start", new_callable=AsyncMock) as mock_start:
        mock_start.return_value = True

        # Test case 2: without limits
        response = client.post("/api/crawler/start", json={
            "platform": "xhs",
            "login_type": "qrcode",
            "crawler_type": "search",
            "keywords": "test"
        })

        assert response.status_code == 200
        mock_start.assert_called_once()
        called_request = mock_start.call_args[0][0]
        assert called_request.platform == PlatformEnum.XHS
        assert called_request.max_notes_count is None
        assert called_request.max_comments_count is None
        assert called_request.max_items_per_minute == 5
        assert called_request.max_concurrency_num == 1


def test_config_options_expose_content_rates():
    client = TestClient(app)

    response = client.get("/api/config/options")

    assert response.status_code == 200
    assert [
        option["value"] for option in response.json()["content_rate_options"]
    ] == [5, 4, 3, 2, 1]
    assert [
        option["value"] for option in response.json()["concurrency_options"]
    ] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("max_notes_count", 0),
        ("max_notes_count", -1),
        ("max_notes_count", 10001),
        ("max_comments_count", 0),
        ("max_comments_count", -1),
        ("max_comments_count", 10001),
        ("max_items_per_minute", 0),
        ("max_items_per_minute", 6),
        ("max_concurrency_num", 0),
        ("max_concurrency_num", 6),
    ],
)
def test_api_rejects_invalid_limits(field_name, value):
    client = TestClient(app)
    payload = {
        "platform": "xhs",
        "login_type": "qrcode",
        "crawler_type": "search",
        "keywords": "test",
        field_name: value,
    }

    with patch("api.routers.crawler.crawler_manager.start", new_callable=AsyncMock) as mock_start:
        response = client.post("/api/crawler/start", json=payload)

    assert response.status_code == 422
    mock_start.assert_not_called()
