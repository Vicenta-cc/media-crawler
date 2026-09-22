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

    try:
        await parse_cmd([
            "--platform", "xhs",
            "--crawler_max_notes_count", "42",
            "--max_comments_count_singlenotes", "24"
        ])
        assert config.CRAWLER_MAX_NOTES_COUNT == 42
        assert config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES == 24
    finally:
        config.CRAWLER_MAX_NOTES_COUNT = orig_notes
        config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = orig_comments


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["general", "most_liked", "latest"])
async def test_cmd_arg_douyin_search_sort(value):
    original = config.DY_SEARCH_SORT
    try:
        result = await parse_cmd([
            "--platform", "dy",
            "--type", "search",
            "--dy_search_sort", value,
        ])
        assert config.DY_SEARCH_SORT == value
        assert result.dy_search_sort == value
    finally:
        config.DY_SEARCH_SORT = original


@pytest.mark.asyncio
async def test_cmd_arg_shared_request_scheduler_limits():
    original = {
        name: getattr(config, name)
        for name in (
            "DY_REQUEST_SCHEDULER_DB",
            "DY_REQUEST_MIN_INTERVAL",
            "DY_REQUESTS_PER_MINUTE",
            "DY_REQUEST_CONCURRENCY",
            "DY_MEDIA_REQUEST_INTERVAL",
            "DY_REQUEST_COOLDOWN_SECONDS",
        )
    }

    try:
        await parse_cmd([
            "--platform", "dy",
            "--request_scheduler_db", "/tmp/douyin-scheduler.sqlite3",
            "--request_min_interval", "1.5",
            "--requests_per_minute", "17",
            "--request_concurrency", "2",
            "--media_request_interval", "4.5",
            "--request_cooldown_seconds", "240",
        ])
        assert config.DY_REQUEST_SCHEDULER_DB == "/tmp/douyin-scheduler.sqlite3"
        assert config.DY_REQUEST_MIN_INTERVAL == 1.5
        assert config.DY_REQUESTS_PER_MINUTE == 17
        assert config.DY_REQUEST_CONCURRENCY == 2
        assert config.DY_MEDIA_REQUEST_INTERVAL == 4.5
        assert config.DY_REQUEST_COOLDOWN_SECONDS == 240
    finally:
        for name, value in original.items():
            setattr(config, name, value)

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

    # 2. Both limits passed in API request
    req2 = CrawlerStartRequest(
        platform=PlatformEnum.XHS,
        login_type=LoginTypeEnum.QRCODE,
        crawler_type=CrawlerTypeEnum.SEARCH,
        keywords="test",
        max_notes_count=50,
        max_comments_count=5
    )
    cmd2 = cm._build_command(req2)
    # Check that they are correctly added
    assert "--crawler_max_notes_count" in cmd2
    idx_notes = cmd2.index("--crawler_max_notes_count")
    assert cmd2[idx_notes + 1] == "50"

    assert "--max_comments_count_singlenotes" in cmd2
    idx_comments = cmd2.index("--max_comments_count_singlenotes")
    assert cmd2[idx_comments + 1] == "5"

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
            "max_comments_count": 5
        })

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "message": "Crawler started successfully"}

        mock_start.assert_called_once()
        called_request = mock_start.call_args[0][0]
        assert called_request.platform == PlatformEnum.XHS
        assert called_request.max_notes_count == 50
        assert called_request.max_comments_count == 5

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


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("max_notes_count", 0),
        ("max_notes_count", -1),
        ("max_notes_count", 10001),
        ("max_comments_count", 0),
        ("max_comments_count", -1),
        ("max_comments_count", 10001),
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
