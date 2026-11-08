"""H1 app fixture (stream-performance): synthetic rows inside a disposable product container.

Run with ``docker exec`` in the product container after it is ready. Creates one
synthetic user (role USER, no password), one synthetic indexer account and a
``torrent_files`` row per synthetic .torrent, then writes stream targets (the
synthetic user's API key and signed stream tokens of this throwaway container)
to ``--out``. Prints only counts, never the key or a token. Uses the product's
own services, so the playback path itself is unchanged.

Usage: python sp_app_fixture.py --torrent-dir /sp/torrents --out /sp/out/targets.json [--indexer ncore]
"""

import argparse
import json
import uuid
from pathlib import Path

from app.common.database import isolated_db_session
from app.modules.indexer_accounts.models import IndexerAccountModel
from app.modules.stream.schemas import StreamToken
from app.modules.stream.utils.stream_token import generate_stream_token
from app.modules.torrent_files.isolated_service import IsolatedTorrentFilesService
from app.modules.users.dependencies import create_users_service
from app.modules.users.schemas.internal import UserCreate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--torrent-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--indexer", default="ncore")
    args = parser.parse_args()

    with isolated_db_session() as db:
        user = create_users_service(db).create(UserCreate(username=f"sp-fixture-{uuid.uuid4().hex[:8]}"))
        api_key = user.api_key
        if db.get(IndexerAccountModel, args.indexer) is None:
            db.add(IndexerAccountModel(indexer_id=args.indexer, username="sp-fixture", password="sp-fixture",
                                       download_full_torrent=False))

    files = IsolatedTorrentFilesService()
    targets = []
    for path in sorted(Path(args.torrent_dir).glob("*.torrent")):
        torrent_id = f"sp-{path.stem}"
        files.create(indexer_id=args.indexer, torrent_id=torrent_id, torrent_bytes=path.read_bytes())
        token = generate_stream_token(StreamToken(indexer_id=args.indexer, torrent_id=torrent_id, file_index=0,
                                                  playback_id=str(uuid.uuid4())))
        targets.append({"id": path.stem, "torrent_id": torrent_id, "token": token})

    out = Path(args.out)
    out.write_text(json.dumps({"api_key": api_key, "indexer": args.indexer, "targets": targets}), encoding="utf-8")
    out.chmod(0o600)
    print(json.dumps({"fixture": "sp-app-fixture-v1", "targets": len(targets)}))


if __name__ == "__main__":
    main()
