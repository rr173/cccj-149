-- Offline usage-interval processor: schema and build functions.
-- All times are int8 UTC milliseconds; all intervals are int8range [lo, hi).

CREATE TABLE IF NOT EXISTS ui_packages (
    package_id   text PRIMARY KEY,
    input_hash   text NOT NULL,
    window_lo    bigint NOT NULL,
    window_hi    bigint NOT NULL,
    status       text NOT NULL CHECK (status IN ('staged', 'built', 'failed')),
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    built_at     timestamptz
);

CREATE TABLE IF NOT EXISTS ui_stg_batches (
    package_id   text NOT NULL REFERENCES ui_packages(package_id),
    batch_idx    int  NOT NULL,
    payload      jsonb NOT NULL,
    PRIMARY KEY (package_id, batch_idx)
);

CREATE TABLE IF NOT EXISTS ui_stg_leases (
    package_id   text NOT NULL REFERENCES ui_packages(package_id),
    lease_id     text NOT NULL,
    device       text NOT NULL,
    start_ms     bigint NOT NULL,
    end_ms       bigint NOT NULL,
    pauses       jsonb NOT NULL,
    PRIMARY KEY (package_id, lease_id, device)
);

CREATE TABLE IF NOT EXISTS ui_stg_endpoints (
    package_id   text NOT NULL REFERENCES ui_packages(package_id),
    device       text NOT NULL,
    session_id   text NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('enter', 'exit')),
    ts           bigint NOT NULL,
    content      text NOT NULL,
    PRIMARY KEY (package_id, device, session_id, kind, ts, content)
);

CREATE TABLE IF NOT EXISTS ui_stg_activities (
    package_id   text NOT NULL REFERENCES ui_packages(package_id),
    activity_id  text NOT NULL,
    device       text NOT NULL,
    lo           bigint NOT NULL,
    hi           bigint NOT NULL,
    PRIMARY KEY (package_id, activity_id, device, lo, hi)
);

CREATE TABLE IF NOT EXISTS ui_stg_coverage (
    package_id   text NOT NULL REFERENCES ui_packages(package_id),
    source       text NOT NULL CHECK (source IN ('door', 'telemetry')),
    lo           bigint NOT NULL,
    hi           bigint NOT NULL,
    PRIMARY KEY (package_id, source, lo, hi)
);

-- ---------------------------------------------------------------- derived --

CREATE TABLE IF NOT EXISTS ui_lease_avail (
    package_id   text NOT NULL,
    device       text NOT NULL,
    lease_id     text NOT NULL,
    range        int8range NOT NULL,   -- already clipped to window, pauses removed
    PRIMARY KEY (package_id, device, lease_id, range)
);

CREATE TABLE IF NOT EXISTS ui_sessions (
    package_id     text NOT NULL,
    device         text NOT NULL,
    session_id     text NOT NULL,
    state          text NOT NULL CHECK (state IN ('complete', 'unpaired', 'conflict')),
    enter_ts       bigint,
    exit_ts        bigint,
    raw_lo         bigint,
    raw_hi         bigint,
    range          int8range,          -- window-clipped meaningful range, NULL if outside
    PRIMARY KEY (package_id, device, session_id)
);

CREATE TABLE IF NOT EXISTS ui_session_endpoints (
    package_id   text NOT NULL,
    device       text NOT NULL,
    session_id   text NOT NULL,
    kind         text NOT NULL,
    ts           bigint NOT NULL,
    contents     text[] NOT NULL,      -- >1 distinct content => conflict
    PRIMARY KEY (package_id, device, session_id, kind, ts)
);

CREATE TABLE IF NOT EXISTS ui_activity_raw (
    package_id   text NOT NULL,
    device       text NOT NULL,
    activity_id  text NOT NULL,
    range        int8range NOT NULL,
    conflict     boolean NOT NULL,     -- same id, different [lo,hi)
    PRIMARY KEY (package_id, device, activity_id, range)
);

CREATE TABLE IF NOT EXISTS ui_activity_union (
    package_id   text NOT NULL,
    device       text NOT NULL,
    range        int8range NOT NULL,
    activity_ids text[] NOT NULL
);

CREATE TABLE IF NOT EXISTS ui_coverage (
    package_id   text NOT NULL,
    device       text NOT NULL,
    source       text NOT NULL,
    range        int8range NOT NULL,
    PRIMARY KEY (package_id, device, source, range)
);

CREATE TABLE IF NOT EXISTS ui_segments (
    package_id        text NOT NULL,
    device            text NOT NULL,
    seg_index         int  NOT NULL,
    range             int8range NOT NULL,
    state             text NOT NULL CHECK (state IN ('conflict', 'confirmed', 'idle', 'unknown')),
    reasons           text[] NOT NULL,
    lease_ids         text[] NOT NULL,
    door_complete     text[] NOT NULL,
    door_unpaired     text[] NOT NULL,
    activity_ids      text[] NOT NULL,
    session_conflicts text[] NOT NULL,
    activity_conflicts text[] NOT NULL,
    door_covered      boolean NOT NULL,
    telemetry_covered boolean NOT NULL,
    PRIMARY KEY (package_id, device, seg_index)
);
CREATE INDEX IF NOT EXISTS ui_segments_pkg ON ui_segments (package_id, device, range);

-- GiST support for the set-based overlap joins during builds (range column)
CREATE INDEX IF NOT EXISTS ui_lease_avail_rng ON ui_lease_avail USING gist (range);
CREATE INDEX IF NOT EXISTS ui_sessions_rng ON ui_sessions USING gist (range);
CREATE INDEX IF NOT EXISTS ui_activity_raw_rng ON ui_activity_raw USING gist (range);
CREATE INDEX IF NOT EXISTS ui_coverage_rng ON ui_coverage USING gist (range);
-- btree for package/device filtering
CREATE INDEX IF NOT EXISTS ui_lease_avail_pd ON ui_lease_avail (package_id, device);
CREATE INDEX IF NOT EXISTS ui_sessions_pd ON ui_sessions (package_id, device);
CREATE INDEX IF NOT EXISTS ui_activity_raw_pd ON ui_activity_raw (package_id, device);
CREATE INDEX IF NOT EXISTS ui_coverage_pd ON ui_coverage (package_id, device);

-- ------------------------------------------------------- range merge agg --
-- Generic int8range union aggregate (merges touching/overlapping ranges).

CREATE OR REPLACE FUNCTION ui_rmerge_state(acc int8multirange, r int8range)
RETURNS int8multirange
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN r IS NULL OR isempty(r) THEN acc
                WHEN acc IS NULL THEN int8multirange(r)
                ELSE acc + int8multirange(r) END;
$$;

CREATE OR REPLACE AGGREGATE ui_range_union(int8range) (
    SFUNC = ui_rmerge_state,
    STYPE = int8multirange
);

-- Convert a multirange to a set of ranges.
CREATE OR REPLACE FUNCTION ui_unnest_mr(mr int8multirange)
RETURNS TABLE (range int8range)
LANGUAGE sql IMMUTABLE AS $$
    SELECT unnest(mr);
$$;

-- ------------------------------------------------------- per-device build --
-- Pure set-based SQL per device.  Python streams only device ids; every fact
-- stays inside the database.

CREATE OR REPLACE FUNCTION ui_build_device(p_pkg text, p_device text)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_lo bigint;
    v_hi bigint;
BEGIN
    SELECT window_lo, window_hi INTO v_lo, v_hi
    FROM ui_packages WHERE package_id = p_pkg;

    -- coverage (shared across devices: the source list has no device column)
    INSERT INTO ui_coverage(package_id, device, source, range)
    SELECT p_pkg, p_device, source, r
    FROM (
        SELECT source, ui_unnest_mr(ui_range_union(int8range(lo, hi, '[)'))) AS r
        FROM ui_stg_coverage
        WHERE package_id = p_pkg
        GROUP BY source
    ) c
    WHERE NOT isempty(r * int8range(v_lo, v_hi, '[)'))
    ON CONFLICT DO NOTHING;

    -- sessions: fold endpoint re-sends, detect content conflicts, pair
    -- across batches.  ORDER BY is only for deterministic raw bounds; the
    -- classification itself is order-independent.
    INSERT INTO ui_session_endpoints(package_id, device, session_id, kind, ts, contents)
    SELECT package_id, device, session_id, kind, ts,
           array_agg(DISTINCT content ORDER BY content)
    FROM ui_stg_endpoints
    WHERE package_id = p_pkg AND device = p_device
    GROUP BY package_id, device, session_id, kind, ts;

    INSERT INTO ui_sessions(package_id, device, session_id, state,
                            enter_ts, exit_ts, raw_lo, raw_hi, range)
    WITH piv AS (
        SELECT session_id,
               (SELECT min(ts) FROM ui_session_endpoints e
                 WHERE e.package_id = p_pkg AND e.device = p_device
                   AND e.session_id = s.session_id AND e.kind = 'enter') AS enter_ts,
               (SELECT min(ts) FROM ui_session_endpoints e
                 WHERE e.package_id = p_pkg AND e.device = p_device
                   AND e.session_id = s.session_id AND e.kind = 'exit') AS exit_ts,
               (SELECT bool_or(coalesce(array_length(contents, 1), 0) > 1)
                  FROM ui_session_endpoints e
                 WHERE e.package_id = p_pkg AND e.device = p_device
                   AND e.session_id = s.session_id) AS has_conflict,
               (SELECT min(ts) FROM ui_session_endpoints e
                 WHERE e.package_id = p_pkg AND e.device = p_device
                   AND e.session_id = s.session_id) AS min_ts,
               (SELECT max(ts) FROM ui_session_endpoints e
                 WHERE e.package_id = p_pkg AND e.device = p_device
                   AND e.session_id = s.session_id) AS max_ts
        FROM (SELECT DISTINCT session_id FROM ui_session_endpoints
               WHERE package_id = p_pkg AND device = p_device) s
    ),
    decided AS (
        SELECT piv.*,
            CASE
              WHEN has_conflict THEN 'conflict'
              WHEN enter_ts IS NOT NULL AND exit_ts IS NOT NULL THEN 'complete'
              ELSE 'unpaired'
            END AS state,
            CASE
              WHEN has_conflict THEN int8range(min_ts, max_ts, '[)')
              WHEN enter_ts IS NOT NULL AND exit_ts IS NOT NULL
                   THEN int8range(enter_ts, exit_ts, '[)')
              WHEN enter_ts IS NOT NULL THEN int8range(enter_ts, v_hi, '[)')
              ELSE int8range(v_lo, exit_ts, '[)')
            END AS candidate
        FROM piv
    )
    SELECT p_pkg, p_device, session_id, state, enter_ts, exit_ts,
           lower(candidate), upper(candidate),
           CASE WHEN isempty(candidate * int8range(v_lo, v_hi, '[)'))
                THEN NULL
                ELSE candidate * int8range(v_lo, v_hi, '[)')
           END
    FROM decided;

    -- activities: same-id different range => business conflict
    INSERT INTO ui_activity_raw(package_id, device, activity_id, range, conflict)
    SELECT p_pkg, p_device, activity_id, int8range(lo, hi, '[)'),
           bool_or(EXISTS (
               SELECT 1 FROM ui_stg_activities x
               WHERE x.package_id = a.package_id AND x.device = a.device
                 AND x.activity_id = a.activity_id
                 AND (x.lo, x.hi) IS DISTINCT FROM (a.lo, a.hi)
           ))
    FROM ui_stg_activities a
    WHERE package_id = p_pkg AND device = p_device
      AND NOT isempty(int8range(lo, hi, '[)') * int8range(v_lo, v_hi, '[)'))
    GROUP BY activity_id, int8range(lo, hi, '[)');

    INSERT INTO ui_activity_union(package_id, device, range, activity_ids)
    SELECT p_pkg, p_device, r,
           (SELECT array_agg(DISTINCT activity_id ORDER BY activity_id)
              FROM ui_activity_raw a
             WHERE a.package_id = p_pkg AND a.device = p_device
               AND a.range && r)
    FROM (
        SELECT ui_unnest_mr(ui_range_union(range)) AS r
        FROM ui_activity_raw
        WHERE package_id = p_pkg AND device = p_device
    ) u
    WHERE NOT isempty(r * int8range(v_lo, v_hi, '[)'));

    -- the full non-overlapping partition, then evidence-based classification
    -- and gap-and-islands merging
    INSERT INTO ui_segments(package_id, device, seg_index, range, state, reasons,
                            lease_ids, door_complete, door_unpaired, activity_ids,
                            session_conflicts, activity_conflicts,
                            door_covered, telemetry_covered)
    WITH
    bounds AS (
        SELECT DISTINCT x AS b FROM (
            SELECT lower(range) AS x FROM ui_lease_avail
             WHERE package_id = p_pkg AND device = p_device
            UNION SELECT upper(range) FROM ui_lease_avail
             WHERE package_id = p_pkg AND device = p_device
            UNION SELECT lower(range) FROM ui_sessions
             WHERE package_id = p_pkg AND device = p_device AND range IS NOT NULL
            UNION SELECT upper(range) FROM ui_sessions
             WHERE package_id = p_pkg AND device = p_device AND range IS NOT NULL
            UNION SELECT lower(range) FROM ui_activity_union
             WHERE package_id = p_pkg AND device = p_device
            UNION SELECT upper(range) FROM ui_activity_union
             WHERE package_id = p_pkg AND device = p_device
            UNION SELECT lower(range) FROM ui_coverage
             WHERE package_id = p_pkg AND device = p_device
            UNION SELECT upper(range) FROM ui_coverage
             WHERE package_id = p_pkg AND device = p_device
            UNION VALUES (v_lo), (v_hi)
        ) z
    ),
    atomic AS (
        SELECT range FROM (
            SELECT int8range(b, lead(b) OVER (ORDER BY b), '[)') AS range
            FROM bounds
        ) q
        WHERE NOT isempty(range)
    ),
    enr AS (
        -- one set-based overlap join per evidence family, then conditional
        -- aggregation; replaces per-segment correlated subqueries.
        SELECT a.range, lower(a.range) AS lo, upper(a.range) AS hi,
          coalesce(l.lease_ids, '{}')          AS lease_ids,
          coalesce(dc.door_complete, '{}')     AS door_complete,
          coalesce(du.door_unpaired, '{}')     AS door_unpaired,
          coalesce(ac.activity_ids, '{}')      AS activity_ids,
          coalesce(sc.session_conflicts, '{}') AS session_conflicts,
          coalesce(act.activity_conflicts, '{}') AS activity_conflicts,
          (coalesce(cd.n, 0) > 0)              AS door_covered,
          (coalesce(ct.n, 0) > 0)              AS telemetry_covered,
          (coalesce(ov.n, 0) > 0)              AS overlapping_complete
        FROM atomic a
        LEFT JOIN LATERAL (
            SELECT array_agg(lease_id ORDER BY lease_id) AS lease_ids
            FROM ui_lease_avail l
            WHERE l.package_id = p_pkg AND l.device = p_device
              AND l.range @> lower(a.range)
        ) l ON true
        LEFT JOIN LATERAL (
            SELECT array_agg(session_id ORDER BY session_id) AS door_complete
            FROM ui_sessions s
            WHERE s.package_id = p_pkg AND s.device = p_device
              AND s.state = 'complete' AND s.range @> lower(a.range)
        ) dc ON true
        LEFT JOIN LATERAL (
            SELECT array_agg(session_id ORDER BY session_id) AS door_unpaired
            FROM ui_sessions s
            WHERE s.package_id = p_pkg AND s.device = p_device
              AND s.state = 'unpaired' AND s.range @> lower(a.range)
        ) du ON true
        LEFT JOIN LATERAL (
            SELECT array_agg(DISTINCT activity_id ORDER BY activity_id) AS activity_ids
            FROM ui_activity_raw r
            WHERE r.package_id = p_pkg AND r.device = p_device
              AND r.range @> lower(a.range)
        ) ac ON true
        LEFT JOIN LATERAL (
            SELECT array_agg(session_id ORDER BY session_id) AS session_conflicts
            FROM ui_sessions s
            WHERE s.package_id = p_pkg AND s.device = p_device
              AND s.state = 'conflict' AND s.range @> lower(a.range)
        ) sc ON true
        LEFT JOIN LATERAL (
            SELECT array_agg(DISTINCT activity_id ORDER BY activity_id) AS activity_conflicts
            FROM ui_activity_raw r
            WHERE r.package_id = p_pkg AND r.device = p_device
              AND r.conflict AND r.range @> lower(a.range)
        ) act ON true
        LEFT JOIN LATERAL (
            SELECT count(*) AS n FROM ui_coverage c
            WHERE c.package_id = p_pkg AND c.device = p_device
              AND c.source = 'door' AND c.range @> lower(a.range)
        ) cd ON true
        LEFT JOIN LATERAL (
            SELECT count(*) AS n FROM ui_coverage c
            WHERE c.package_id = p_pkg AND c.device = p_device
              AND c.source = 'telemetry' AND c.range @> lower(a.range)
        ) ct ON true
        LEFT JOIN LATERAL (
            SELECT count(*) AS n
            FROM ui_sessions s1 JOIN ui_sessions s2
              ON s1.package_id = s2.package_id AND s1.device = s2.device
             AND s1.session_id < s2.session_id
             AND s1.state = 'complete' AND s2.state = 'complete'
             AND s1.range && s2.range
            WHERE s1.package_id = p_pkg AND s1.device = p_device
              AND int8range(greatest(lower(s1.range), lower(s2.range)),
                            least(upper(s1.range), upper(s2.range)), '[)')
                  @> lower(a.range)
        ) ov ON true
        WHERE NOT isempty(a.range)
    ),
    classified AS (
        SELECT *,
            (SELECT coalesce(array_agg(r ORDER BY r), '{}') FROM unnest(
                CASE
                  WHEN overlapping_complete OR coalesce(array_length(session_conflicts, 1), 0) > 0
                       OR coalesce(array_length(activity_conflicts, 1), 0) > 0
                    THEN ARRAY['door_or_activity_conflict']
                  ELSE '{}' END
                || CASE WHEN coalesce(array_length(lease_ids, 1), 0) > 1 THEN ARRAY['multiple_usable_leases'] ELSE '{}' END
                || CASE WHEN (coalesce(array_length(door_complete, 1), 0) > 0
                              OR coalesce(array_length(activity_ids, 1), 0) > 0)
                              AND coalesce(array_length(lease_ids, 1), 0) = 0
                         THEN ARRAY['usage_without_lease'] ELSE '{}' END
            ) AS r) AS conflict_reasons
        FROM enr
    ),
    stated AS (
        SELECT *,
          CASE WHEN coalesce(array_length(conflict_reasons, 1), 0) > 0 THEN 'conflict'
               WHEN coalesce(array_length(door_unpaired, 1), 0) > 0 THEN 'unknown'
               WHEN coalesce(array_length(lease_ids, 1), 0) = 1
                    AND coalesce(array_length(door_complete, 1), 0) > 0
                    AND coalesce(array_length(activity_ids, 1), 0) > 0 THEN 'confirmed'
               WHEN coalesce(array_length(door_complete, 1), 0) = 0
                    AND coalesce(array_length(door_unpaired, 1), 0) = 0
                    AND coalesce(array_length(activity_ids, 1), 0) = 0
                    AND (door_covered = true) AND (telemetry_covered = true) THEN 'idle'
               ELSE 'unknown'
          END AS state,
          CASE WHEN coalesce(array_length(conflict_reasons, 1), 0) > 0 THEN conflict_reasons
               WHEN coalesce(array_length(door_unpaired, 1), 0) > 0 THEN ARRAY['unpaired_door_endpoint']
               WHEN coalesce(array_length(lease_ids, 1), 0) = 1
                    AND coalesce(array_length(door_complete, 1), 0) > 0
                    AND coalesce(array_length(activity_ids, 1), 0) > 0
                    THEN ARRAY['confirmed_lease_door_and_telemetry']
               WHEN coalesce(array_length(door_complete, 1), 0) = 0
                    AND coalesce(array_length(door_unpaired, 1), 0) = 0
                    AND coalesce(array_length(activity_ids, 1), 0) = 0
                    AND (door_covered = true) AND (telemetry_covered = true)
                    THEN ARRAY['no_evidence_both_collections_complete']
               ELSE ARRAY['incomplete_or_partial_evidence']
          END AS reasons
        FROM classified
    ),
    islands AS (
        SELECT *,
               count(*) FILTER (WHERE newgrp) OVER (ORDER BY lo) AS grp
        FROM (
            SELECT s.*,
                   (lag(lo) OVER (ORDER BY lo) IS NULL
                    OR lag(state) OVER (ORDER BY lo) IS DISTINCT FROM state
                    OR lag(reasons) OVER (ORDER BY lo) IS DISTINCT FROM reasons
                    OR lag(lease_ids) OVER (ORDER BY lo) IS DISTINCT FROM lease_ids
                    OR lag(door_complete) OVER (ORDER BY lo) IS DISTINCT FROM door_complete
                    OR lag(door_unpaired) OVER (ORDER BY lo) IS DISTINCT FROM door_unpaired
                    OR lag(activity_ids) OVER (ORDER BY lo) IS DISTINCT FROM activity_ids
                    OR lag(session_conflicts) OVER (ORDER BY lo) IS DISTINCT FROM session_conflicts
                    OR lag(activity_conflicts) OVER (ORDER BY lo) IS DISTINCT FROM activity_conflicts
                    OR lag(door_covered) OVER (ORDER BY lo) IS DISTINCT FROM door_covered
                    OR lag(telemetry_covered) OVER (ORDER BY lo) IS DISTINCT FROM telemetry_covered
                   ) AS newgrp
            FROM stated s
        ) q
    )
    SELECT p_pkg, p_device,
           row_number() OVER (ORDER BY grp) - 1,
           int8range(min(lo), max(hi), '[)'),
           min(state),
           (SELECT reasons FROM islands i2 WHERE i2.grp = islands.grp LIMIT 1),
           (SELECT lease_ids FROM islands i2 WHERE i2.grp = islands.grp LIMIT 1),
           (SELECT door_complete FROM islands i2 WHERE i2.grp = islands.grp LIMIT 1),
           (SELECT door_unpaired FROM islands i2 WHERE i2.grp = islands.grp LIMIT 1),
           (SELECT activity_ids FROM islands i2 WHERE i2.grp = islands.grp LIMIT 1),
           (SELECT session_conflicts FROM islands i2 WHERE i2.grp = islands.grp LIMIT 1),
           (SELECT activity_conflicts FROM islands i2 WHERE i2.grp = islands.grp LIMIT 1),
           (SELECT door_covered FROM islands i2 WHERE i2.grp = islands.grp LIMIT 1),
           (SELECT telemetry_covered FROM islands i2 WHERE i2.grp = islands.grp LIMIT 1)
    FROM islands
    GROUP BY grp;
END;
$$;

-- ------------------------------------------------------------- full build --

CREATE OR REPLACE FUNCTION ui_build_package(p_pkg text)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_lo bigint;
    v_hi bigint;
    d text;
BEGIN
    SELECT window_lo, window_hi INTO v_lo, v_hi
    FROM ui_packages WHERE package_id = p_pkg;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown package %', p_pkg; END IF;

    -- reset derived facts for a (re)build; staging stays untouched
    DELETE FROM ui_segments WHERE package_id = p_pkg;
    DELETE FROM ui_coverage WHERE package_id = p_pkg;
    DELETE FROM ui_activity_union WHERE package_id = p_pkg;
    DELETE FROM ui_activity_raw WHERE package_id = p_pkg;
    DELETE FROM ui_sessions WHERE package_id = p_pkg;
    DELETE FROM ui_session_endpoints WHERE package_id = p_pkg;
    DELETE FROM ui_lease_avail WHERE package_id = p_pkg;

    -- leases minus pauses, clipped to the window, as multiranges
    INSERT INTO ui_lease_avail(package_id, device, lease_id, range)
    SELECT package_id, device, lease_id, r
    FROM (
        SELECT l.package_id, l.device, l.lease_id,
               ui_unnest_mr(
                   int8multirange(
                       int8range(greatest(l.start_ms, v_lo),
                                 least(l.end_ms, v_hi), '[)'))
                   - coalesce((
                       SELECT ui_range_union(int8range((p->>0)::bigint,
                                                        (p->>1)::bigint, '[)'))
                       FROM jsonb_array_elements(l.pauses) p
                   )::int8multirange, int8multirange()::int8multirange)
               ) AS mr
        FROM ui_stg_leases l
        WHERE l.package_id = p_pkg
          AND greatest(l.start_ms, v_lo) < least(l.end_ms, v_hi)
    ) z CROSS JOIN LATERAL ui_unnest_mr(z.mr::int8multirange) AS r
    WHERE NOT isempty(r);

    -- every device that has any staged fact, streamed one at a time
    FOR d IN
        SELECT DISTINCT device FROM (
            SELECT device FROM ui_stg_leases WHERE package_id = p_pkg
            UNION
            SELECT device FROM ui_stg_endpoints WHERE package_id = p_pkg
            UNION
            SELECT device FROM ui_stg_activities WHERE package_id = p_pkg
        ) all_devices
    LOOP
        PERFORM ui_build_device(p_pkg, d);
    END LOOP;

    UPDATE ui_packages
       SET status = 'built', built_at = now(), error = NULL
     WHERE package_id = p_pkg;
END;
$$;
