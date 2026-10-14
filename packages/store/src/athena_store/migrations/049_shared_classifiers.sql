ALTER TABLE symbol_facts ADD COLUMN IF NOT EXISTS classifiers text NOT NULL DEFAULT '';

ALTER TABLE symbol_facts ADD COLUMN IF NOT EXISTS thesis text NOT NULL DEFAULT '';

UPDATE symbol_facts sf
SET classifiers = CASE WHEN sf.classifiers = '' THEN src.classifiers ELSE sf.classifiers END,
    thesis = CASE WHEN sf.thesis = '' THEN src.thesis ELSE sf.thesis END,
    updated_at = now()
FROM (
    SELECT DISTINCT ON (symbol)
        symbol,
        classifiers,
        thesis
    FROM symbol_phases
    WHERE classifiers <> '' OR thesis <> ''
    ORDER BY symbol, updated_at DESC NULLS LAST
) src
WHERE sf.symbol = src.symbol
  AND (sf.classifiers = '' OR sf.thesis = '');

INSERT INTO symbol_facts (symbol, name, market_cap, classifiers, thesis)
SELECT src.symbol, '', NULL, src.classifiers, src.thesis
FROM (
    SELECT DISTINCT ON (symbol)
        symbol,
        classifiers,
        thesis
    FROM symbol_phases
    WHERE classifiers <> '' OR thesis <> ''
    ORDER BY symbol, updated_at DESC NULLS LAST
) src
WHERE NOT EXISTS (
    SELECT 1 FROM symbol_facts sf WHERE sf.symbol = src.symbol
);
