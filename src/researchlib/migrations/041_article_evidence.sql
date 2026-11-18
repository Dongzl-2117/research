-- 迁移前的去重旧稿不是连续时间线。对现存文章只记统一起点，修改时才归档上一版本的正文。
LOCK TABLE articles IN ACCESS EXCLUSIVE MODE;
-- 取拿到锁以后的时钟。等待旧写入提交的时间不能算成已知版本；常量默认值无需重写现有正文。
DO $$
BEGIN
    EXECUTE format('ALTER TABLE articles ADD COLUMN IF NOT EXISTS evidence_written_at timestamptz NOT NULL DEFAULT %L',
                   clock_timestamp());
END;
$$;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS evidence_xid xid8;

-- 写事务以外的会话能看见其记录后，才有证据证明新版本在该时点已经提交。
CREATE TABLE IF NOT EXISTS article_evidence_commits (
    xid xid8 PRIMARY KEY,
    confirmed_at timestamptz
);
CREATE INDEX IF NOT EXISTS article_evidence_pending ON article_evidence_commits (xid) WHERE confirmed_at IS NULL;

CREATE TABLE IF NOT EXISTS article_evidence_history (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    article_id bigint NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    written_at timestamptz NOT NULL,
    written_xid xid8,
    replaced_at timestamptz NOT NULL,
    source_id text NOT NULL,
    title text NOT NULL,
    published_at timestamptz,
    body_md text NOT NULL,
    raw_meta jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS article_evidence_at ON article_evidence_history (article_id, replaced_at);

CREATE OR REPLACE FUNCTION record_article_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    changed_at timestamptz := clock_timestamp();
    writing_xid xid8 := pg_current_xact_id();
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF ROW(OLD.source_id, OLD.title, OLD.published_at, OLD.body_md, OLD.raw_meta->'author_replies',
               OLD.raw_meta->'poster_role', OLD.raw_meta->'hidden') IS NOT DISTINCT FROM
           ROW(NEW.source_id, NEW.title, NEW.published_at, NEW.body_md, NEW.raw_meta->'author_replies',
               NEW.raw_meta->'poster_role', NEW.raw_meta->'hidden') THEN
            RETURN NEW;
        END IF;
        -- 同一事务里写了好几次，外部只能看到最终结果。
        IF OLD.evidence_xid = writing_xid THEN
            RETURN NEW;
        END IF;
        INSERT INTO article_evidence_history
            (article_id, written_at, written_xid, replaced_at, source_id, title, published_at, body_md, raw_meta)
        VALUES (OLD.id, OLD.evidence_written_at, OLD.evidence_xid, changed_at, OLD.source_id,
                OLD.title, OLD.published_at, OLD.body_md, OLD.raw_meta);
    END IF;
    INSERT INTO article_evidence_commits (xid) VALUES (writing_xid) ON CONFLICT DO NOTHING;
    NEW.evidence_written_at := changed_at;
    NEW.evidence_xid := writing_xid;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS article_evidence_write ON articles;
CREATE TRIGGER article_evidence_write BEFORE INSERT OR UPDATE OF source_id, title, published_at, body_md, raw_meta
    ON articles FOR EACH ROW EXECUTE FUNCTION record_article_evidence();
