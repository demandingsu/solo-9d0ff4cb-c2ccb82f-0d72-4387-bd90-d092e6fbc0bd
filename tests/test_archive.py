import asyncio
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as web
from app.scraper import (
    ArchiveUnavailable,
    ImageFailure,
    ScanSource,
    _classify_image,
    build_archive,
    inspect_issue,
)
from sample_site.main import app as sample_app


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sample_app),
        base_url="http://sample.test",
        follow_redirects=True,
    )


async def _report(client: httpx.AsyncClient, slug: str):
    return await inspect_issue(f"http://sample.test/issues/{slug}/page/1.html", client=client)


@pytest.mark.asyncio
async def test_qualified_issue_archive_contains_images_and_manifest():
    async with make_client() as client:
        report = await _report(client, "clean")
        assert report.status == "合格"
        package, failures = await build_archive(report, client=client)

    assert failures == []
    assert package is not None
    assert package.filename.endswith(".zip")

    with zipfile.ZipFile(package.path) as archive:
        names = archive.namelist()
        manifest = archive.read("manifest.csv").decode("utf-8")

    # One file per accepted detail: 5 details, even though one covers pages 2-3.
    assert names[0] == "manifest.csv"
    assert len([name for name in names if name.startswith("scans/")]) == 5
    assert all(name.startswith("scans/") for name in names[1:])
    assert "期号,版次,页码,详情地址,包内文件名" in manifest
    assert "2-3" in manifest
    # The spread detail is listed once, with a single image file.
    assert manifest.count("a2.html") == 1

    package.cleanup()
    assert not package.path.exists()


@pytest.mark.asyncio
async def test_broken_images_produce_failures_and_no_package():
    async with make_client() as client:
        report = await _report(client, "broken-image")
        assert report.status == "合格"  # page coverage alone is qualified
        package, failures = await build_archive(report, client=client)

    assert package is None
    assert len(failures) == 2
    messages = " ".join(failure.message for failure in failures)
    assert "HTTP 404" in messages
    assert "text/html" in messages

    # Failed builds leave no temporary files behind.
    for workdir in Path("/tmp").glob("scan-archive-*"):
        assert not workdir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["conflict", "pending"])
async def test_unqualified_issue_is_not_archived(slug):
    async with make_client() as client:
        report = await _report(client, slug)
        with pytest.raises(ArchiveUnavailable):
            await build_archive(report, client=client)


@pytest.mark.asyncio
async def test_same_detail_is_stored_only_once():
    async with make_client() as client:
        report = await _report(client, "clean")
        duplicate = ScanSource(
            code="S99",
            detail_url=report.accepted_scans[1].detail_url,
            image_url=report.accepted_scans[1].image_url,
            pages_text=report.accepted_scans[1].pages_text,
            page_numbers=list(report.accepted_scans[1].page_numbers),
            issue=report.accepted_scans[1].issue,
            edition=report.accepted_scans[1].edition,
        )
        report.accepted_scans.append(duplicate)
        package, failures = await build_archive(report, client=client)

    assert failures == []
    with zipfile.ZipFile(package.path) as archive:
        image_names = [name for name in archive.namelist() if name.startswith("scans/")]
    assert len(image_names) == 5
    assert len(package.manifest_rows) == 5
    package.cleanup()


def test_non_image_responses_are_rejected():
    assert _classify_image("text/html", b"<!doctype html><html>maintenance</html>") == (
        "",
        "图片响应不是图片内容（Content-Type: text/html）",
    )
    assert _classify_image("image/png", b"<html>not a png</html>") == (
        "",
        "图片响应内容与声明的 image/png 不符",
    )
    assert _classify_image("image/svg+xml", b'\xef\xbb\xbf<?xml version="1.0"?><svg></svg>') == (
        "svg",
        "",
    )
    assert _classify_image("application/octet-stream", b"\x89PNG\r\n\x1a\nrest") == ("png", "")
    assert _classify_image("text/plain", b"\x89PNG\r\n\x1a\nrest") == (
        "",
        "图片响应不是图片内容（Content-Type: text/plain）",
    )


@pytest.mark.asyncio
async def test_image_is_streamed_to_disk_before_zipping(tmp_path, monkeypatch):
    """The download step must write chunks to a file instead of buffering."""
    report = await _report_for_streaming_test()
    chunks = [b"\x89PNG\r\n\x1a\n", b"A" * 100, b"B" * 100]

    async def handler(request: httpx.Request) -> httpx.Response:
        async def body():
            for chunk in chunks:
                yield chunk

        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=body(),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    async with client:
        package, failures = await build_archive(report, client=client)

    assert failures == []
    assert package is not None
    with zipfile.ZipFile(package.path) as archive:
        image = archive.read(f"scans/{package.images[0].scan.code}.png")
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) == sum(len(chunk) for chunk in chunks)
    # The ZIP is assembled from files on disk.
    assert package.images[0].size == len(image)
    package.cleanup()


async def _report_for_streaming_test():
    from app.scraper import PageFinding, IssueReport

    scan = ScanSource(
        code="S01",
        detail_url="http://sample.test/scan/stream/1.html",
        image_url="http://sample.test/img/stream-1.png",
        pages_text="1",
        page_numbers=[1],
        issue="2024-05-01",
        edition="第3版",
    )
    return IssueReport(
        directory_url="http://sample.test/issues/stream/page/1.html",
        issue_no="2024-05-01",
        edition="第3版",
        total_pages=1,
        pages=[PageFinding(number=1, sources=[scan])],
        accepted_scans=[scan],
        ignored_scans=[],
        failures=[],
        directory_pages=["http://sample.test/issues/stream/page/1.html"],
    )


def test_web_lists_archive_failures_and_keeps_page_verdict(monkeypatch):
    report = asyncio.run(_report_for_streaming_test())
    failures = [
        ImageFailure(
            image_url="http://sample.test/img/stream-1.png",
            detail_url="http://sample.test/scan/stream/1.html",
            message="图片请求失败：HTTP 404",
        )
    ]

    async def fake_inspect(url):
        return report

    async def fake_build(report_arg):
        return None, failures

    monkeypatch.setattr(web, "inspect_issue", fake_inspect)
    monkeypatch.setattr(web, "build_archive", fake_build)

    with TestClient(web.app) as client:
        response = client.post("/archive", data={"url": "http://sample.test/issues/stream/page/1.html"})

    assert response.status_code == 200
    body = response.text
    assert "归档未生成" in body
    assert "归档图片失败来源" in body
    assert "图片请求失败：HTTP 404" in body
    # The original page verdict is still rendered separately.
    assert "逐页判定与扫描来源" in body
    assert "第 1 页" in body
    assert "已覆盖" in body


@pytest.mark.asyncio
async def test_incomplete_read_is_not_archived(monkeypatch):
    async with make_client() as client:
        report = await _report(client, "clean")
    report.read_complete = False
    with pytest.raises(ArchiveUnavailable, match="未完整读取"):
        await build_archive(report)
