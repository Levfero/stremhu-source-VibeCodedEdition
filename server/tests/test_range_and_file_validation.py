import pytest
from fastapi import HTTPException

from app.modules.stream.service import parse_range_header, validate_file_type


def test_parse_range_header_rfc9110_matrix():
    n = 1000

    # 1. nincs Range -> teljes fájl
    r = parse_range_header(file_size=n, range_header=None)
    assert r.start_byte == 0
    assert r.end_byte == 999
    assert r.content_length == 1000

    # 2. bytes=0-99 -> 100 bájt
    r = parse_range_header(file_size=n, range_header="bytes=0-99")
    assert r.start_byte == 0
    assert r.end_byte == 99
    assert r.content_length == 100

    # 3. bytes=900- -> utolsó 100 bájt
    r = parse_range_header(file_size=n, range_header="bytes=900-")
    assert r.start_byte == 900
    assert r.end_byte == 999
    assert r.content_length == 100

    # 4. bytes=-100 -> utolsó 100 bájt (suffix)
    r = parse_range_header(file_size=n, range_header="bytes=-100")
    assert r.start_byte == 900
    assert r.end_byte == 999
    assert r.content_length == 100

    # 5. bytes=900-5000 -> végpont levágása 999-re
    r = parse_range_header(file_size=n, range_header="bytes=900-5000")
    assert r.start_byte == 900
    assert r.end_byte == 999
    assert r.content_length == 100

    # 6. bytes=1000- -> 416 nem kielégíthető tartomány
    with pytest.raises(HTTPException) as exc_info:
        parse_range_header(file_size=n, range_header="bytes=1000-")
    assert exc_info.value.status_code == 416
    assert exc_info.value.headers == {"Content-Range": "bytes */1000"}

    # 7. Szintaktikailag hibás range-ek -> szabályozott 416 (nem 500)
    for invalid in ["bytes=abc-def", "bytes=", "bytes=invalid", "invalid", "bytes=50-20"]:
        with pytest.raises(HTTPException) as exc_info:
            parse_range_header(file_size=n, range_header=invalid)
        assert exc_info.value.status_code == 416
        assert exc_info.value.headers == {"Content-Range": "bytes */1000"}

    # 8. Több tartomány (multipart) elutasítása -> szabályozott 416
    with pytest.raises(HTTPException) as exc_info:
        parse_range_header(file_size=n, range_header="bytes=0-50, 100-150")
    assert exc_info.value.status_code == 416
    assert exc_info.value.headers == {"Content-Range": "bytes */1000"}


def test_validate_file_type():
    # Videó vagy felirat stream végpontra: engedélyezett
    validate_file_type(is_video=True, is_subtitle=False, require_subtitle=False)
    validate_file_type(is_video=False, is_subtitle=True, require_subtitle=False)

    # Nem médiafájl (pl. .nfo, .txt, .exe): elutasítva 400-zal
    with pytest.raises(HTTPException) as exc:
        validate_file_type(is_video=False, is_subtitle=False, require_subtitle=False)
    assert exc.value.status_code == 400

    # Felirat végpontra csak felirat engedélyezett:
    validate_file_type(is_video=False, is_subtitle=True, require_subtitle=True)

    with pytest.raises(HTTPException) as exc:
        validate_file_type(is_video=True, is_subtitle=False, require_subtitle=True)
    assert exc.value.status_code == 400
    assert "nem felirat" in exc.value.detail

