-- Review decisions on investments flagged "Review": kept after a fresh look, or sold.
ALTER TABLE plan_actions DROP CONSTRAINT plan_actions_action_check;
ALTER TABLE plan_actions ADD CONSTRAINT plan_actions_action_check
    CHECK (action IN ('done', 'partly', 'skipped', 'kept', 'sold'));
