-- Food app database (SQLite). Contract shared by T8 (ingest), T9 (hours), T10 (tags). Do not change without telling Claude.
PRAGMA foreign_keys = ON;

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

-- Budget slider: prices move in $3 steps. A dish's price_band is the smallest multiple of 3 that is >= its price
-- (price 9.00 -> 9, price 9.01 -> 12). Selecting slider value S shows dishes with price <= S (same as price_band <= S).
-- 3..36 covers about 98.7% of priced items. There are no price tags: bands replace them.
INSERT INTO meta (key, value) VALUES ('price_step', '3'), ('slider_min', '3'), ('slider_max', '36');

CREATE TABLE places (
  id               INTEGER PRIMARY KEY,
  name             TEXT NOT NULL UNIQUE,
  area             TEXT NOT NULL,            -- one of the 11 area labels used in the workbook
  stop_line        TEXT,                     -- nearest T stop and line as written in the workbook
  address          TEXT,
  cuisine          TEXT,                     -- free text from the workbook
  price_tier       TEXT CHECK (price_tier IN ('$','$$','$$$') OR price_tier IS NULL),
  late_night       TEXT NOT NULL CHECK (late_night IN ('yes','likely','no','not_checked')),
  hours_text       TEXT,                     -- "Opening hours (local time)" column, verbatim
  hours_status     TEXT NOT NULL DEFAULT 'none' CHECK (hours_status IN ('parsed','partial','unparsed','none','closed')),  -- partial: only some days/times could be read
  open_late        INTEGER CHECK (open_late IN (0,1)),          -- closes at/after 01:00 on any day; NULL if hours unknown
  open_past_midnight INTEGER CHECK (open_past_midnight IN (0,1)), -- closes after 00:00 on any day; NULL if unknown
  menu_status      TEXT NOT NULL CHECK (menu_status IN ('menu_found','no_public_menu','closed','skipped')),
  menu_owner_id    INTEGER REFERENCES places(id),               -- branch shares another place's menu (NULL = own menu)
  notes            TEXT,
  source_group     TEXT,                     -- 'Late-night list' | 'New research' | 'Original list'
  maps_url         TEXT,
  website_url      TEXT,
  lat              REAL,
  lng              REAL
);

-- One row per open interval. weekday: Monday=0 .. Sunday=6. Minutes from local midnight of that weekday.
-- close_min may exceed 1440 when the place closes after midnight (e.g. 2am next day = 1560).
CREATE TABLE place_hours (
  id        INTEGER PRIMARY KEY,
  place_id  INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE,
  weekday   INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
  open_min  INTEGER NOT NULL CHECK (open_min BETWEEN 0 AND 1439),
  close_min INTEGER NOT NULL CHECK (close_min BETWEEN 1 AND 2880 AND close_min > open_min),
  kind      TEXT NOT NULL DEFAULT 'main' CHECK (kind IN ('main','dine_in','takeout','kitchen','bar')),
  raw_text  TEXT
);

CREATE TABLE menu_items (
  id           INTEGER PRIMARY KEY,
  place_id     INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE,  -- the place that OWNS this menu
  category     TEXT,
  name         TEXT NOT NULL,
  price        REAL NOT NULL CHECK (price > 0 AND price < 500),
  source_url   TEXT,
  price_status TEXT NOT NULL CHECK (price_status IN ('ok','verify')),    -- workbook Status: OK -> ok, Verify (...) -> verify
  price_note   TEXT,                                                       -- the text inside Verify (...)
  notes        TEXT
);

CREATE TABLE tags (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  kind        TEXT NOT NULL CHECK (kind IN ('dietary','meal','food_type','flavor','price','cuisine','vibe')),
  description TEXT
);

CREATE TABLE place_tags (
  place_id   INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE,
  tag_id     INTEGER NOT NULL REFERENCES tags(id),
  source     TEXT NOT NULL,                  -- e.g. 'rule:cuisine-keyword', 'derived:hours', 'sheet'
  confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  PRIMARY KEY (place_id, tag_id)
);

CREATE TABLE item_tags (
  item_id    INTEGER NOT NULL REFERENCES menu_items(id) ON DELETE CASCADE,
  tag_id     INTEGER NOT NULL REFERENCES tags(id),
  source     TEXT NOT NULL,                  -- e.g. 'rule:name-keyword', 'derived:price'
  confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  PRIMARY KEY (item_id, tag_id)
);

CREATE INDEX idx_menu_items_place ON menu_items(place_id);
CREATE INDEX idx_menu_items_price ON menu_items(price);
CREATE INDEX idx_place_hours_place ON place_hours(place_id, weekday);
CREATE INDEX idx_places_area ON places(area);
CREATE INDEX idx_item_tags_tag ON item_tags(tag_id);
CREATE INDEX idx_place_tags_tag ON place_tags(tag_id);

-- Every dish a place can serve, following shared menus (branches use menu_owner_id).
CREATE VIEW v_place_dishes AS
SELECT p.id AS place_id, p.name AS place_name, p.area, p.stop_line, p.address, p.price_tier,
       p.late_night, p.open_late, p.open_past_midnight, p.hours_text,
       i.id AS item_id, i.category, i.name AS item_name, i.price, i.price_status, i.source_url,
       ((CAST(ROUND(i.price * 100) AS INTEGER) + 299) / 300) * 3 AS price_band
FROM places p
JOIN menu_items i ON i.place_id = COALESCE(p.menu_owner_id, p.id)
WHERE p.menu_status = 'menu_found';

CREATE VIEW v_open_late AS
SELECT id AS place_id, name, area, stop_line, hours_text FROM places WHERE open_late = 1;
