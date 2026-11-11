-- 002: review_log records where the review note was written and its frontmatter totals.
ALTER TABLE review_log ADD COLUMN path TEXT;
ALTER TABLE review_log ADD COLUMN totals_json TEXT;

INSERT OR IGNORE INTO schema_version (version) VALUES (2);
