UPDATE settings_versions
SET payload = jsonb_set(
        payload,
        '{risk,min_rv_pctl_new_short}',
        payload #> '{risk,min_iv_rank_new_short}'
    ) #- '{risk,min_iv_rank_new_short}'
WHERE payload #>> '{risk,min_iv_rank_new_short}' IS NOT NULL;
