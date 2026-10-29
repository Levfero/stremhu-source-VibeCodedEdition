from __future__ import annotations

import asyncio
from pathlib import Path
import time
from typing import Annotated

import content_types
from fastapi import (
    APIRouter,
    Depends,
    Header,
    Request,
    Response,
)
from fastapi.responses import StreamingResponse

from app.common.database import isolated_db_session
from app.common.schemas.internal import ImdbInfo
from app.modules.auth.dependencies import create_auth_service
from app.modules.playback_histories.dependencies import (
    create_playback_histories_service,
)
from app.modules.playback_histories.schemas.internal import (
    PlaybackHistoryClientInfo,
)
from app.modules.playbacks.dependencies import create_playbacks_service
from app.modules.relay.dependencies import get_relay_service
from app.modules.stream.active_playback import record_active_playback
from app.modules.stream.dependencies import (
    create_stream_service,
    get_parsed_stream_token,
)
from app.modules.stream.schemas import StreamToken
from app.modules.stream.utils.subtitles import ass_to_vtt

router = APIRouter(
    prefix="/{api_key}/stream",
    tags=["Stream"],
)

subtitles_router = APIRouter(
    prefix="/{api_key}/subtitles",
    tags=["Subtitles"],
)


@router.get(
    "/{stream_token}",
)
@router.head(
    "/{stream_token}",
)
async def stream(
    request: Request,
    api_key: str,
    stream_token: Annotated[StreamToken, Depends(get_parsed_stream_token)],
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    record_active_playback(
        api_key=api_key,
        indexer_id=stream_token.indexer_id,
        torrent_id=stream_token.torrent_id,
    )

    client_info = PlaybackHistoryClientInfo(
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )

    with isolated_db_session() as local_db:
        auth_service = create_auth_service(local_db)
        user = await asyncio.to_thread(
            auth_service.verify_api_key,
            api_key=api_key,
        )

        playbacks_service = create_playbacks_service(
            relay_service=get_relay_service(),
            playback_histories_service=create_playback_histories_service(local_db),
        )
        playbacks_service.check_playback_limit(
            user=user,
            current_playback_id=stream_token.playback_id,
        )
        stream_service = create_stream_service(local_db)

        parsed_range_header, file = await stream_service.prepare_for_stream(
            range_header=range_header,
            indexer_id=stream_token.indexer_id,
            torrent_id=stream_token.torrent_id,
            file_index=stream_token.file_index,
        )

        imdb_info: ImdbInfo | None = None
        if stream_token.imdb_id is not None:
            imdb_info = ImdbInfo(
                imdb_id=stream_token.imdb_id,
                series_info=stream_token.series_info,
            )

        await stream_service.save_playback_history(
            stream_token=stream_token,
            client_info=client_info,
            user_id=user.id,
            file=file,
            imdb_info=imdb_info,
        )

    content_type = content_types.get_content_type(file.name)
    media_type = content_type or "application/octet-stream"

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store, no-transform",
        "Connection": "close",
    }

    if range_header is None:
        status_code = 200
        headers["Content-Length"] = str(file.size)
    else:
        status_code = 206
        headers["Content-Length"] = str(parsed_range_header.content_length)
        headers["Content-Range"] = (
            f"bytes {parsed_range_header.start_byte}-{parsed_range_header.end_byte}/{file.size}"
        )

    if request.method == "HEAD":
        return _apply_capitalized_headers(
            Response(
                status_code=status_code,
                headers=headers,
                media_type=media_type,
            )
        )

    iterator = await file.stream(
        playback_id=stream_token.playback_id,
        user_id=user.id,
        request=request,
        stream_start_byte=parsed_range_header.start_byte,
        stream_end_byte=parsed_range_header.end_byte,
    )

    return _apply_capitalized_headers(
        StreamingResponse(
            content=iterator,
            media_type=media_type,
            status_code=status_code,
            headers=headers,
        )
    )


_SUBTITLE_CACHE_MAX_ENTRIES = 128
_SUBTITLE_CACHE_TTL_SECONDS = 3600.0
# Key: (indexer_id, torrent_id, file_index, want_vtt) -> (timestamp, media_type, disp_name, content_bytes)
_subtitle_content_cache: dict[
    tuple[str, str, int, bool], tuple[float, str, str, bytes]
] = {}


def _get_cached_subtitle(
    cache_key: tuple[str, str, int, bool],
) -> tuple[str, str, bytes] | None:
    entry = _subtitle_content_cache.get(cache_key)
    if entry is None:
        return None
    ts, media_type, disp_name, content_bytes = entry
    if time.monotonic() - ts > _SUBTITLE_CACHE_TTL_SECONDS:
        _subtitle_content_cache.pop(cache_key, None)
        return None
    return media_type, disp_name, content_bytes


def _put_cached_subtitle(
    cache_key: tuple[str, str, int, bool],
    media_type: str,
    disp_name: str,
    content_bytes: bytes,
) -> None:
    if len(_subtitle_content_cache) >= _SUBTITLE_CACHE_MAX_ENTRIES:
        _subtitle_content_cache.pop(next(iter(_subtitle_content_cache)), None)
    _subtitle_content_cache[cache_key] = (
        time.monotonic(),
        media_type,
        disp_name,
        content_bytes,
    )


def _build_cached_subtitle_response(
    request_method: str,
    media_type: str,
    disp_name: str,
    content_bytes: bytes,
    range_header: str | None,
    convert_to_vtt: bool,
    stream_service,
) -> Response:
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=86400",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
        "Content-Disposition": f'inline; filename="{disp_name}"',
    }
    total_size = len(content_bytes)
    if range_header is None or convert_to_vtt or total_size == 0:
        status_code = 200
        body = content_bytes
        headers["Content-Length"] = str(total_size)
    else:
        parsed_range = stream_service._parse_range_header(
            file_size=total_size,
            range_header=range_header,
        )
        status_code = 206
        body = content_bytes[parsed_range.start_byte : parsed_range.end_byte + 1]
        headers["Content-Length"] = str(len(body))
        headers["Content-Range"] = (
            f"bytes {parsed_range.start_byte}-{parsed_range.end_byte}/{total_size}"
        )

    if request_method == "HEAD":
        return _apply_capitalized_headers(
            Response(
                status_code=status_code,
                headers=headers,
                media_type=media_type,
            )
        )
    return _apply_capitalized_headers(
        Response(
            content=body,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
        )
    )


@subtitles_router.get("/{stream_token}")
@subtitles_router.get("/{stream_token}/{filename}")
@subtitles_router.head("/{stream_token}")
@subtitles_router.head("/{stream_token}/{filename}")
async def stream_subtitle(
    request: Request,
    api_key: str,
    stream_token: Annotated[StreamToken, Depends(get_parsed_stream_token)],
    filename: str | None = None,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    req_ext_hint = Path(filename).suffix.lower() if filename else ""
    want_vtt_hint = req_ext_hint == ".vtt"
    cache_key = (
        stream_token.indexer_id,
        stream_token.torrent_id,
        stream_token.file_index,
        want_vtt_hint,
    )

    with isolated_db_session() as local_db:
        auth_service = create_auth_service(local_db)
        user = await asyncio.to_thread(
            auth_service.verify_api_key,
            api_key=api_key,
        )

        stream_service = create_stream_service(local_db)

        cached = _get_cached_subtitle(cache_key)
        if cached is not None:
            cached_media_type, cached_disp_name, cached_bytes = cached
            return _build_cached_subtitle_response(
                request_method=request.method,
                media_type=cached_media_type,
                disp_name=cached_disp_name,
                content_bytes=cached_bytes,
                range_header=range_header,
                convert_to_vtt=want_vtt_hint,
                stream_service=stream_service,
            )

        parsed_range_header, file = await stream_service.prepare_for_stream(
            range_header=range_header,
            indexer_id=stream_token.indexer_id,
            torrent_id=stream_token.torrent_id,
            file_index=stream_token.file_index,
        )

    req_ext = (
        Path(filename).suffix.lower() if filename else Path(file.name).suffix.lower()
    )
    file_ext = Path(file.name).suffix.lower()

    convert_to_vtt = req_ext == ".vtt" and file_ext in (".ass", ".ssa")
    final_cache_key = (
        stream_token.indexer_id,
        stream_token.torrent_id,
        stream_token.file_index,
        convert_to_vtt,
    )

    if convert_to_vtt:
        media_type = "text/vtt; charset=utf-8"
        disp_name = Path(file.name).stem + ".vtt"
    elif file_ext in (".ass", ".ssa"):
        media_type = "text/x-ssa; charset=utf-8"
        disp_name = file.name
    elif file_ext == ".vtt":
        media_type = "text/vtt; charset=utf-8"
        disp_name = file.name
    elif file_ext == ".srt":
        media_type = "text/plain; charset=utf-8"
        disp_name = file.name
    else:
        media_type = (
            content_types.get_content_type(file.name) or "text/plain; charset=utf-8"
        )
        disp_name = file.name

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=86400",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
        "Content-Disposition": f'inline; filename="{disp_name}"',
    }

    if convert_to_vtt:
        if request.method == "HEAD":
            return _apply_capitalized_headers(
                Response(
                    status_code=200,
                    headers=headers,
                    media_type=media_type,
                )
            )

        iterator = await file.stream(
            playback_id=stream_token.playback_id,
            user_id=user.id,
            request=request,
            stream_start_byte=0,
            stream_end_byte=file.size - 1,
        )
        chunks = []
        async for chunk in iterator:
            chunks.append(chunk)
        raw_bytes = b"".join(chunks)
        content_text = raw_bytes.decode("utf-8-sig", errors="replace")
        vtt_output = ass_to_vtt(content_text)
        vtt_bytes = vtt_output.encode("utf-8")
        _put_cached_subtitle(final_cache_key, media_type, disp_name, vtt_bytes)
        if cache_key != final_cache_key:
            _put_cached_subtitle(cache_key, media_type, disp_name, vtt_bytes)
        headers["Content-Length"] = str(len(vtt_bytes))
        return _apply_capitalized_headers(
            Response(
                content=vtt_bytes,
                status_code=200,
                headers=headers,
                media_type=media_type,
            )
        )

    if range_header is None:
        status_code = 200
        headers["Content-Length"] = str(file.size)
    else:
        status_code = 206
        headers["Content-Length"] = str(parsed_range_header.content_length)
        headers["Content-Range"] = (
            f"bytes {parsed_range_header.start_byte}-{parsed_range_header.end_byte}/{file.size}"
        )

    if request.method == "HEAD":
        return _apply_capitalized_headers(
            Response(
                status_code=status_code,
                headers=headers,
                media_type=media_type,
            )
        )

    if file.size <= 2 * 1024 * 1024:
        iterator = await file.stream(
            playback_id=stream_token.playback_id,
            user_id=user.id,
            request=request,
            stream_start_byte=0,
            stream_end_byte=file.size - 1,
        )
        chunks = []
        async for chunk in iterator:
            chunks.append(chunk)
        raw_bytes = b"".join(chunks)
        _put_cached_subtitle(final_cache_key, media_type, disp_name, raw_bytes)
        if cache_key != final_cache_key:
            _put_cached_subtitle(cache_key, media_type, disp_name, raw_bytes)
        return _build_cached_subtitle_response(
            request_method=request.method,
            media_type=media_type,
            disp_name=disp_name,
            content_bytes=raw_bytes,
            range_header=range_header,
            convert_to_vtt=False,
            stream_service=stream_service,
        )

    iterator = await file.stream(
        playback_id=stream_token.playback_id,
        user_id=user.id,
        request=request,
        stream_start_byte=parsed_range_header.start_byte,
        stream_end_byte=parsed_range_header.end_byte,
    )

    return _apply_capitalized_headers(
        StreamingResponse(
            content=iterator,
            media_type=media_type,
            status_code=status_code,
            headers=headers,
        )
    )



def _apply_capitalized_headers(response: Response) -> Response:
    overrides = {
        b"content-length": b"Content-Length",
        b"content-range": b"Content-Range",
        b"accept-ranges": b"Accept-Ranges",
        b"cache-control": b"Cache-Control",
        b"content-type": b"Content-Type",
        b"connection": b"Connection",
    }
    headers = []
    for key, value in response.raw_headers:
        headers.append((overrides.get(key, key), value))
    response.raw_headers = headers
    return response
