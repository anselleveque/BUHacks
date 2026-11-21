-- Example queries for the dish picker. Run: sqlite3 data/foodapp.db < db/example_queries.sql
-- Build the database first:  python3 -m foodapp.build_db

-- 1) A random dish: budget slider S = 12, vegetarian, from a place open past midnight, no drinks or bulk items.
SELECT d.place_name, d.item_name, d.price, d.price_band, d.area, d.stop_line
FROM v_place_dishes d
JOIN places p ON p.id = d.place_id
WHERE d.price <= 12                      -- slider value (3, 6, ... 36)
  AND p.open_past_midnight = 1           -- drop this line for "any time"
  AND EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id
              WHERE it.item_id = d.item_id AND t.name = 'vegetarian')     -- dietary need (explicit tags only)
  AND NOT EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id
                  WHERE it.item_id = d.item_id AND t.name IN ('beverage', 'shareable'))
ORDER BY RANDOM() LIMIT 1;

-- 2) How many dishes each slider step unlocks (all places).
SELECT price_band, COUNT(*) AS dishes FROM v_place_dishes WHERE price_band <= 36 GROUP BY 1 ORDER BY 1;

-- 3) Late-night places with their closing days (close_min over 1440 means after midnight; 1560 = 2 AM).
SELECT p.name, p.area, h.weekday, h.open_min / 60.0 AS opens, h.close_min / 60.0 AS closes
FROM places p JOIN place_hours h ON h.place_id = p.id
WHERE p.open_late = 1 AND h.close_min >= 1500
ORDER BY p.name, h.weekday LIMIT 20;
