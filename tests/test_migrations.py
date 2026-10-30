import sqlite3

from hotload.config import Settings
from hotload.db import Database


async def test_fresh_database_gets_full_schema(tmp_path):
    db = Database(Settings(data_dir=tmp_path))
    await db.migrate()
    tables = {r[0] for r in sqlite3.connect(tmp_path / "hotload.db").execute("select name from sqlite_master where type='table'")}
    assert {"users", "history", "payments", "api_keys", "blocklist", "reports", "alembic_version"} <= tables
    await db.migrate()  # running again is a no-op
    await db.dispose()


async def test_m2_database_is_adopted_and_keeps_its_data(tmp_path):
    """A database created by M2's create_all (no tier_expires, no M3 tables) must upgrade in place."""
    path = tmp_path / "hotload.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE users (id VARCHAR(32) PRIMARY KEY, email VARCHAR(320) NOT NULL, password_hash VARCHAR(255),
            tier VARCHAR(16) NOT NULL DEFAULT 'free', oauth_provider VARCHAR(16), oauth_sub VARCHAR(128),
            created FLOAT NOT NULL, UNIQUE (oauth_provider, oauth_sub));
        CREATE UNIQUE INDEX ix_users_email ON users (email);
        CREATE TABLE history (id VARCHAR(32) PRIMARY KEY, user_id VARCHAR(32) NOT NULL, url VARCHAR(2048) NOT NULL,
            title VARCHAR(300), site VARCHAR(64), preset VARCHAR(16) NOT NULL, state VARCHAR(10) NOT NULL DEFAULT 'queued',
            created FLOAT NOT NULL);
        INSERT INTO users (id, email, tier, created) VALUES ('u1', 'old@example.com', 'premium', 1.0);
        INSERT INTO history (id, user_id, url, preset, created) VALUES ('h1', 'u1', 'https://example.com/v', 'mp3', 2.0);
    """)
    con.commit()
    con.close()
    db = Database(Settings(data_dir=tmp_path))
    await db.migrate()
    user = await db.user_by_email("old@example.com")
    assert user.tier == "premium" and user.tier_expires is None
    assert (await db.list_history("u1"))[0]["id"] == "h1"
    cols = {r[1] for r in sqlite3.connect(path).execute("pragma table_info(users)")}
    assert "tier_expires" in cols
    await db.dispose()
