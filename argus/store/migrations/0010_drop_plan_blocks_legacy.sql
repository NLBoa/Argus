-- 0010: planned sessions are gone for good (D060). Applied only after the worker has carried their
-- passed work over as `legacy` time; `store.db.migrate` holds it back until then.
DROP TABLE plan_blocks_legacy;
