"""Additive website tables. Existing league tables remain authoritative."""
WEB_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_server_registry (
 guild_id INTEGER PRIMARY KEY, added_by INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS web_leagues (
 guild_id INTEGER PRIMARY KEY, public_enabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS web_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
 season TEXT, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT,
 audience TEXT NOT NULL, home_user_id INTEGER, away_user_id INTEGER,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_events_guild ON web_events(guild_id,id);
CREATE TABLE IF NOT EXISTS web_event_reads (
 guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, through_id INTEGER NOT NULL,
 PRIMARY KEY(guild_id,user_id)
);
CREATE TABLE IF NOT EXISTS web_requests (
 guild_id INTEGER NOT NULL, actor_id INTEGER NOT NULL, request_key TEXT NOT NULL,
 body_hash TEXT NOT NULL, response TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(guild_id,actor_id,request_key)
);
CREATE TABLE IF NOT EXISTS web_record_adjustments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, season TEXT NOT NULL,
 team_name TEXT NOT NULL, actor_id INTEGER NOT NULL, wins_delta INTEGER NOT NULL,
 losses_delta INTEGER NOT NULL, ties_delta INTEGER NOT NULL, reason TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS web_live_streams (
 guild_id INTEGER NOT NULL, platform TEXT NOT NULL, channel_key TEXT NOT NULL,
 live_id TEXT NOT NULL, user_id INTEGER NOT NULL, team_name TEXT NOT NULL,
 title TEXT NOT NULL, url TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 PRIMARY KEY(guild_id,platform,channel_key)
);
CREATE TABLE IF NOT EXISTS web_outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, kind TEXT NOT NULL,
 entity_id INTEGER, payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 delivered_at TEXT, last_error TEXT, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS web_reminder_events AFTER INSERT ON reminder_deliveries
BEGIN
 INSERT INTO web_events (guild_id,season,kind,title,body,audience,home_user_id,away_user_id,created_at)
 SELECT guild_id,season,'reminder',away_team || ' @ ' || home_team,
 'Game reminder: ' || NEW.milestone,'owners',home_user_id,away_user_id,NEW.delivered_at
 FROM matchups WHERE id=NEW.matchup_id;
END;
CREATE TRIGGER IF NOT EXISTS web_stream_started AFTER INSERT ON web_live_streams
BEGIN
 INSERT INTO web_events (guild_id,kind,title,body,audience,created_at)
 VALUES (NEW.guild_id,'live_stream',NEW.title,NEW.team_name || ' is live on ' || NEW.platform,'public',NEW.last_seen_at);
END;
CREATE TRIGGER IF NOT EXISTS web_stream_changed AFTER UPDATE ON web_live_streams
WHEN NEW.live_id != OLD.live_id
BEGIN
 INSERT INTO web_events (guild_id,kind,title,body,audience,created_at)
 VALUES (NEW.guild_id,'live_stream',NEW.title,NEW.team_name || ' is live on ' || NEW.platform,'public',NEW.last_seen_at);
END;
CREATE TRIGGER IF NOT EXISTS web_matchup_events AFTER UPDATE ON matchups
WHEN OLD.status != NEW.status OR OLD.scheduled_at IS NOT NEW.scheduled_at
BEGIN
 INSERT INTO web_events (guild_id,season,kind,title,body,audience,home_user_id,away_user_id,created_at)
 VALUES (NEW.guild_id,NEW.season,NEW.status,NEW.away_team || ' @ ' || NEW.home_team,
 CASE WHEN NEW.status IN ('complete','force_home','force_away','fair_sim')
 THEN COALESCE(NEW.away_score,'—') || ' – ' || COALESCE(NEW.home_score,'—') ELSE NULL END,
 CASE WHEN NEW.status IN ('scheduled','complete','force_home','force_away','fair_sim')
 THEN 'public' ELSE 'owners' END,NEW.home_user_id,NEW.away_user_id,NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS web_audit_events AFTER INSERT ON audit_logs
WHEN NEW.action IN ('announcements','manual_week_rollover','automatic_week_rollover',
 'stream_request_approved','stream_request_rejected','team_assigned','team_claim_approved')
BEGIN
 INSERT INTO web_events (guild_id,season,kind,title,body,audience,home_user_id,created_at)
 VALUES (NEW.guild_id,(SELECT season FROM guild_settings WHERE guild_id=NEW.guild_id),
 NEW.action,COALESCE(json_extract(NEW.details,'$.title'),replace(NEW.action,'_',' ')),
 json_extract(NEW.details,'$.body'),
 CASE WHEN NEW.action IN ('announcements','manual_week_rollover','automatic_week_rollover')
 THEN 'public' ELSE 'owners' END,
 CASE WHEN NEW.target_type='member' THEN CAST(NEW.target_id AS INTEGER) ELSE NEW.actor_id END,
 NEW.created_at);
END;
"""
